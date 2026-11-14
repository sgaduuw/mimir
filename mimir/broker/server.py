"""Broker daemon: queue + worker-pool architecture.

The broker accepts an arbitrary number of concurrent client
connections. Each connection runs a small **reader thread** that
pulls JSONL request lines off the socket and enqueues them onto a
work queue. RPC workers submit writes to one SQLite writer and reply
from the worker that dispatched them.

Why this shape (rather than `ThreadingMixIn` per-connection):

1. **Multi-client serving is the load-bearing property.** Earlier
   versions ran `socketserver.UnixStreamServer` without threading,
   so once one persistent connection was accept()ed (e.g. gunicorn
   worker #1), all other clients sat unread by the application
   even though the kernel had queued their connects. With two
   gunicorn workers + scheduler-tasks subprocesses, the un-served
   clients eventually timed out on `recv`. The reader-per-
   connection design fixes that: every connection is being read
   from by its own thread.

2. **Writes stay serialised at one writer.** RPC workers submit
   WriteOps to the WriterThread, which commits them serially. The
   default single cache worker preserves submission order for cache sets.

3. **Backpressure is observable.** Queue depth and per-RPC
   queue-wait time are first-class signals. The slow-RPC WARNING
   that 1.32.x already emits gets a queue-wait breakdown for
   free (`Nms total = Qms queued + Dms dispatch`), so an
   operator looking at a slow log line can tell whether the
   broker is contended at the front of the queue (many clients
   piling on) or at the back (SQLite writer lock held by
   scheduler-side ingest).

4. **Future-ready for batching.** A queue is the natural place
   to coalesce N cache.sets into one transaction when the
   throughput needs it. Not done; a future optimisation.

The periodic-purge thread submits the expired-row DELETE through the
broker's WriterThread on `PURGE_INTERVAL_SEC` cadence so every write
funnels through the single-writer invariant. A write on a separate
connection would race intra-broker writers and silently skip ticks on
`SQLITE_BUSY_SNAPSHOT`.

Signal handlers (`SIGTERM`, `SIGINT`) set `stop_event`; reader
threads notice via their selector poll, the worker notices via
its `queue.get(timeout=...)` poll. Shutdown attempts a bounded worker
drain and warns if cache replies remain unfinished.
"""

import faulthandler
import logging
import os
import pickle
import queue
import re
import selectors
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
import tracemalloc
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from mimir import cache
from mimir.broker import _context, startup
from mimir.broker.handlers import (
    LONG_OPS,
    WARM_OPS,
    classify_op,
    dispatch,
    is_vacuum_active,
)
from mimir.broker.pools import ReadSessionPool
from mimir.broker.protocol import Reply
from mimir.broker.watchdog import InFlightJobs
from mimir.broker.writes import WriterThread
from mimir.config import settings

logger = logging.getLogger(__name__)


PURGE_INTERVAL_SEC = 60  # Preserve minute-scale cleanup without a warming client.

# How often the worker / reader threads check `stop_event` while idle.
# Bounds the shutdown latency from SIGTERM to clean exit.
SHUTDOWN_POLL_SEC = 0.1

# Hard cap on a single JSONL request line (bytes). The reader closes
# the connection if a peer pushes more than this without a newline.
# Bounds memory growth from two related local-DoS shapes: a
# never-terminating line that exhausts broker memory via the linebuf,
# and a deeply-nested JSON-bomb payload that expands at parse time.
#
# 16 MiB accommodates the largest real cache values observed today
# (sitemap XML payloads for popular inboxes land around 2 MiB; the
# 1.33.x large-payload regression test pins a 2 MiB round-trip) and
# leaves an order of magnitude of headroom for growth. Still 3x
# tighter than the parser's MAX_RAW_MESSAGE_BYTES. The Phase E
# application-level cap on `CacheSetRequest.value_json` adds an
# 8 MiB ceiling on top of this wire cap.
MAX_REQUEST_BYTES = 16 * 1024 * 1024

# Per-connection inactivity ceiling (seconds). A connection that
# accepts a slot in the kernel queue but sends no bytes for this long
# is closed so it can't pin a reader thread + selector indefinitely.
# Queued/running requests are not idle. The window restarts when the
# last response completes, so long operations need no keepalive.
IDLE_TIMEOUT_SEC = 300
# Bound response delivery separately from arbitrarily long handler execution.
REPLY_TIMEOUT_SEC = 30
# Reader-side priority extractor for warm RPCs. Sub-ms per line; the
# warm queue is a `queue.PriorityQueue` and routing on the wire-side
# numeric priority is what makes fast-tier RPCs (sitemap-class) dequeue
# ahead of queued slow-tier ones (Task 5 of the fast/slow tier split,
# spec §2 §5). Implemented as a small regex against the head of the
# line rather than full JSON parse so the reader thread stays sub-ms
# per RPC and never blocks the accept loop's enqueue rate. Falls back
# to 1 (slow) on missing field / malformed digits, matching the
# protocol-level default.
_WARM_PRIORITY_RE = re.compile(rb'"priority"\s*:\s*(\d+)')


def _extract_warm_priority(line: bytes) -> int:
    """Cheaply parse a warm RPC JSONL line for its priority field.
    Defaults to 1 (slow) when absent, matching the protocol-level
    default on `WarmInboxRequest` / `WarmGlobalRequest`. Bounded
    by inspecting only the first 512 bytes of the line; the
    `priority` field rides up near the front of any well-formed
    warm request, and capping the search keeps the per-RPC cost
    bounded regardless of `targets=` payload size."""
    m = _WARM_PRIORITY_RE.search(line[:512])
    if m is None:
        return 1
    try:
        return int(m.group(1))
    except ValueError:
        return 1


@dataclass
class ClientConnection:
    """Per-accepted-connection state carried alongside the socket
    on every queue tuple. Owns the `send_lock` that workers acquire
    around `sock.sendall(reply)` so concurrent replies from N
    workers do not interleave bytes on the wire (3.0.0 pipelining).
    The reader thread owns the socket's read side; workers only
    write. Socket sends have a timeout so a stalled reader releases the worker."""

    sock: socket.socket
    send_lock: threading.Lock = field(default_factory=threading.Lock)
    # Keep idle checks independent of a potentially blocked socket send.
    activity_lock: threading.Lock = field(default_factory=threading.Lock)
    pending: int = 0
    last_activity: float = field(default_factory=time.monotonic)


@dataclass
class _ReplyDelivery:
    line: bytes
    conn: ClientConnection
    queue: queue.Queue
    worker_tag: str
    queue_wait_ms: float
    started: float


# Linux exposes SO_PEERCRED (pid + uid + gid of the peer process) on
# AF_UNIX sockets via getsockopt. macOS / BSDs have their own (and
# different) peer-credential interfaces; we don't try to bridge them
# because production is Linux and dev tests don't need the check to
# pass. When the symbol is absent we skip the check and rely on the
# socket file mode + parent-dir ownership as the access control.
_HAS_SO_PEERCRED = hasattr(socket, "SO_PEERCRED")

# Cached at module load: the euid we expect every peer to share.
# Captured once because `os.geteuid()` is cheap but the check fires
# on every accept and there's no reason to re-read on every call.
_BROKER_EUID = os.geteuid()


class _BrokerServer(socketserver.UnixStreamServer):
    """UNIX-socket server that hands every accepted connection off
    to a dedicated reader thread. Three RPC worker classes drain
    their own queues:

    - `cache_queue` for cache-set admission and ping. Sets acknowledge
      admission without waiting for commit. A full writer queue drops the set.
      Pinned by `test_web_tier_rpcs_all_route_to_the_cache_queue`.
      `broker_cache_workers` workers (default 1, env
      `BROKER_CACHE_WORKERS`); the single-worker default preserves
      FIFO submission order per client.
    - `long_queue` for long-running ops (`bootstrap_inboxes` in
      Phase 2.0; ingest / backfills / mainline / analyze / vacuum
      in Phase 2.1+). `broker_long_workers` workers (default 1,
      env `BROKER_LONG_WORKERS`); long ops funnel at the
      WriterThread regardless of worker count, so bumping the
      default rarely improves wall time.
    - `warm_queue` for cache warming (Phase 2.2). `broker_warm_workers`
      workers (default 4, env `BROKER_WARM_WORKERS`) so the
      compute phase of warming overlaps across inboxes; cache.set
      commits still funnel through the SQLite writer lock but
      every warm worker has its own session for the read phase.

    All worker threads ultimately funnel their SQLite writes
    through one WriterThread (Phase 4+), so cross-queue write
    contention is bounded at the writer rather than at each
    queue's individual workers.

    `daemon_threads = True` so reader / worker threads don't block
    process exit on unclean shutdown.

    `request_queue_size` (the kernel's `listen()` backlog) stays at
    256 (from 1.32.3) so brief bursts above the accept rate don't
    surface as EAGAIN at the client.
    """

    request_queue_size = 256

    def __init__(self, socket_path: str) -> None:
        # The handler class is never instantiated: `process_request`
        # below hands each connection to a reader thread instead, so
        # the framework's base class satisfies the required argument.
        super().__init__(socket_path, socketserver.BaseRequestHandler)
        self.stop_event = threading.Event()
        # Three queues, one per worker class. Items are
        # `(line, conn, enqueued_at)` tuples; the reader classifies
        # by op name and routes (`handlers.classify_op` ->
        # `handlers.LONG_OPS` / `WARM_OPS`). Unbounded queues:
        # under observed peak load (a few hundred cache.sets per
        # warm-cache tick) memory is negligible and a hard cap
        # would just drop work silently. Slow cache/long RPCs and
        # failed warm replies warn with queue-vs-dispatch breakdown;
        # successful warm replies retain those details at DEBUG.
        self.cache_queue: queue.Queue[tuple[bytes, ClientConnection, float]] = (
            queue.Queue()
        )
        self.long_queue: queue.Queue[tuple[bytes, ClientConnection, float]] = (
            queue.Queue()
        )
        # Warm queue is a PriorityQueue so fast-tier RPCs
        # (priority=0; sitemap-class) jump ahead of queued slow-tier
        # (priority=1; subsystem dashboards + tracker). Tuple shape:
        # (priority, enqueued_at, line, conn). The enqueued_at slot
        # serves both as the slow-RPC queue-wait input AND as the
        # FIFO tie-breaker between two items at the same priority
        # (PriorityQueue compares tuples element-wise, so equal
        # priority falls through to enqueued_at). Task 5 of the
        # fast/slow tier split (spec §2 §5).
        self.warm_queue: queue.PriorityQueue[
            tuple[int, float, bytes, ClientConnection]
        ] = queue.PriorityQueue()
        self._reader_threads: list[threading.Thread] = []
        # Held around `_reader_threads` mutations so the per-reader
        # self-removal in `_reader_loop`'s finally block does not race
        # against the accept-loop's append. Held microseconds per call.
        self._reader_threads_lock = threading.Lock()
        self._cache_worker_threads: list[threading.Thread] = []
        self._long_worker_threads: list[threading.Thread] = []
        self._warm_worker_threads: list[threading.Thread] = []
        # The two-pool restructure
        # (_claude/specs/2026-05-29-broker-two-pool-design.md): handlers
        # dispatch reads through `self.read_pool.session()` and writes
        # via `self.writer.submit(op)` (migration complete as of
        # Phase 6). Owned by the server so lifecycle ordering (start
        # before accept, stop after drain) is explicit.
        self.read_pool = ReadSessionPool.from_settings()
        self.writer = WriterThread.from_settings()
        # What each worker is running, for the stuck-job watchdog
        # (`mimir.broker.watchdog`, #1123).
        self.in_flight = InFlightJobs()
        self._watchdog_thread: threading.Thread | None = None

    def process_request(self, request, client_address) -> None:
        """Override of `BaseServer.process_request` so each accepted
        connection runs on its own thread instead of synchronously
        on the accept loop. The reader thread owns the socket
        lifecycle (closes it on EOF / error / shutdown); we do NOT
        call `shutdown_request` here for that reason.

        Peer-credential check: on Linux we read SO_PEERCRED and
        refuse connections from any euid other than our own. The
        socket file mode (0660) and parent-dir ownership are the
        primary gate; this is defense-in-depth against a misconfigured
        deploy that loosens the mode, against a co-tenant on the
        same UID-group, or against a future deploy that puts the
        socket somewhere with broader visibility. On non-Linux the
        check is a no-op."""
        if not _check_peer_uid(request):
            try:
                request.close()
            except OSError:
                pass
            return
        thread = threading.Thread(
            target=self._reader_loop,
            args=(request,),
            daemon=True,
            name=f"broker-reader-{request.fileno()}",
        )
        with self._reader_threads_lock:
            self._reader_threads.append(thread)
        thread.start()

    def handle_error(self, request, client_address) -> None:
        # Suppress socketserver's default print-to-stderr; reader
        # threads catch and log their own errors. Without this
        # override an EOF mid-readline would smear a stack trace
        # across the broker's log.
        logger.exception("broker: connection error")

    def _reader_loop(self, sock: socket.socket) -> None:
        """Read JSONL request lines from this connection, classify
        each by op name (`handlers.LONG_OPS` → long_queue; otherwise
        cache_queue), and enqueue tagged with the enqueue timestamp.
        One reader per accepted connection.

        Classification is a cheap JSON peek (`classify_op`) plus a
        set membership test. Malformed lines and unknown ops route
        to the cache queue and let `dispatch` produce a structured
        failure reply, preserving existing error semantics.

        Two hostile-peer defenses bound the work this loop does:

        - **`MAX_REQUEST_BYTES` cap on the linebuf.** A peer that
          streams bytes without ever sending `\\n` would otherwise
          grow `linebuf` until the broker process OOMs. Crossing the
          cap closes the connection on the offending peer.
        - **`IDLE_TIMEOUT_SEC` ceiling on inactivity.** A peer that
          accepts a slot and has no outstanding requests would otherwise
          pin this reader thread + its selector indefinitely. Beyond the
          ceiling the connection is closed.

        Oversized requests log at WARNING; routine idle expiry logs at
        DEBUG so normal connection cleanup does not bury warnings."""
        # Set once, before sharing the socket with workers. recv follows a
        # readability check; sendall gets a deadline even if this reader is busy.
        sock.settimeout(REPLY_TIMEOUT_SEC)
        conn = ClientConnection(sock=sock)
        linebuf = bytearray()
        sel = selectors.DefaultSelector()
        sel.register(sock, selectors.EVENT_READ)
        try:
            while not self.stop_event.is_set():
                events = sel.select(timeout=SHUTDOWN_POLL_SEC)
                if not events:
                    with conn.activity_lock:
                        idle = (
                            conn.pending == 0
                            and time.monotonic() - conn.last_activity > IDLE_TIMEOUT_SEC
                        )
                    if idle:
                        logger.debug(
                            "broker: closing idle connection after %ds",
                            IDLE_TIMEOUT_SEC,
                        )
                        return
                    continue
                try:
                    chunk = sock.recv(4096)
                # PEP 758 (Python 3.14): the bare-comma form is the
                # canonical tuple-of-exceptions syntax; `ruff format`
                # rewrites parenthesised forms to this shape. NOT a
                # Python 2 holdover (audit #482, closed as not-a-bug).
                except OSError, ConnectionError:
                    return
                if not chunk:
                    return  # Clean EOF from peer.
                with conn.activity_lock:
                    conn.last_activity = time.monotonic()
                linebuf.extend(chunk)
                if len(linebuf) > MAX_REQUEST_BYTES:
                    logger.warning(
                        "broker: closing connection; "
                        "request line exceeded %d bytes without newline",
                        MAX_REQUEST_BYTES,
                    )
                    return
                while True:
                    nl = linebuf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(linebuf[:nl])
                    del linebuf[: nl + 1]
                    op = classify_op(line)
                    enqueued_at = time.perf_counter()
                    # Count before enqueueing: queue wait is active work too.
                    with conn.activity_lock:
                        conn.pending += 1
                    if op is not None and op in LONG_OPS:
                        self.long_queue.put((line, conn, enqueued_at))
                    elif op is not None and op in WARM_OPS:
                        # Warm queue is a PriorityQueue; tuple shape
                        # is (priority, enqueued_at, line, conn).
                        # Fast-tier RPCs (priority=0) jump ahead of
                        # queued slow-tier (priority=1); enqueued_at
                        # is the FIFO tie-breaker within a priority
                        # class. See `_extract_warm_priority` for the
                        # sub-ms regex extractor.
                        priority = _extract_warm_priority(line)
                        self.warm_queue.put((priority, enqueued_at, line, conn))
                    else:
                        self.cache_queue.put((line, conn, enqueued_at))
        finally:
            sel.close()
            try:
                sock.close()
            except OSError:
                pass
            # Self-remove from `_reader_threads` so the list does not
            # grow unboundedly with connection churn. The accept loop
            # holds the same lock around its append; ValueError is
            # tolerated because a shutdown sweep could also prune
            # this thread between its death and this finally block.
            with self._reader_threads_lock:
                try:
                    self._reader_threads.remove(threading.current_thread())
                except ValueError:
                    pass

    def _worker_loop(
        self,
        q: queue.Queue,
        worker_tag: str,
    ) -> None:
        """Drain one queue serially. One RPC at a time on this
        worker so writes stay ordered at the SQLAlchemy layer
        without an extra lock. Each iteration: dequeue, dispatch,
        write reply to the originating socket. Slow-RPC WARNING
        fires with queue-wait + dispatch breakdown, except successful
        warm replies whose handlers report their own timing at INFO.

        `worker_tag` tags the slow-RPC log line ("cache" / "long" /
        "warm") so operators reading the broker log can tell which
        queue is contended without inferring it from the op string.

        Queue tuple shapes differ by queue (Task 5 + Task 6):

        - `cache_queue` / `long_queue`: `(line, conn, enqueued_at)`.
        - `warm_queue` is a PriorityQueue with shape
          `(priority, enqueued_at, line, conn)`. The first slot is
          the int priority (0=fast, 1=slow) so PriorityQueue's
          element-wise tuple compare gives fast items dispatch
          precedence over slow items at the same `enqueued_at`.

        `conn` is a `ClientConnection` wrapping the socket plus a
        per-connection `send_lock`. Workers acquire the lock around
        `conn.sock.sendall(payload)` so concurrent replies from N
        workers (e.g. multiple warm workers replying to the same
        connection) do not interleave bytes on the wire.

        In-flight slow ops are NOT preempted: once a worker picks
        an item via `q.get()`, the dispatch runs to completion
        before the next `get()` returns whatever's on top. Priority
        is a queue-ordering policy, not a cancellation primitive.
        """
        while not self.stop_event.is_set():
            try:
                item = q.get(timeout=SHUTDOWN_POLL_SEC)
            except queue.Empty:
                continue
            # Outer try/except guarantees an unhandled exception from
            # dispatch / sendall / unpack does not kill the worker
            # thread and silently stop draining the queue (audit #478).
            # The inner try/finally still fires `q.task_done()` so
            # `q.join()` stays correct.
            try:
                if q is self.warm_queue:
                    _priority, enqueued_at, line, conn = item
                else:
                    line, conn, enqueued_at = item
                try:
                    self.in_flight.start(worker_tag, line)
                    delivery = _ReplyDelivery(
                        line=line,
                        conn=conn,
                        queue=q,
                        worker_tag=worker_tag,
                        queue_wait_ms=(time.perf_counter() - enqueued_at) * 1000.0,
                        started=time.perf_counter(),
                    )
                    # Mark this worker thread as "currently dispatching a
                    # broker handler" so any reachable `cache.set` avoids a
                    # self-RPC back through the socket and instead routes
                    # through the active WriterThread (the in-broker
                    # single-writer path).
                    # Production handles the same concern via
                    # `MIMIR_IS_BROKER=true` env at the process level, but
                    # the in-process test environment shares
                    # `settings.mimir_is_broker` between broker thread and
                    # CLI invocations; the thread-local flag is the right
                    # granularity in either case.
                    cache._set_broker_handler_active(True)
                    try:
                        reply = dispatch(line)
                    finally:
                        cache._set_broker_handler_active(False)
                    outcome = self._deliver_reply(reply, delivery)
                    self._log_reply(reply, delivery, outcome)
                finally:
                    self.in_flight.finish()
                    self._finish_request(conn, q)
            except Exception:
                logger.exception(
                    "broker [%s]: worker caught unhandled exception; "
                    "continuing to drain queue",
                    worker_tag,
                )

    def _log_reply(self, reply: Reply, delivery: _ReplyDelivery, outcome: str) -> None:
        dispatch_ms = (time.perf_counter() - delivery.started) * 1000.0
        total_ms = delivery.queue_wait_ms + dispatch_ms

        threshold_ms = settings.broker_slow_rpc_warn_ms
        successful_warm = (
            delivery.queue is self.warm_queue and reply.ok and outcome == "sent"
        )
        if threshold_ms > 0 and total_ms >= threshold_ms and not successful_warm:
            # Breakdown helps operators tell the
            # difference between front-of-queue contention
            # (high queue_wait_ms; many clients piling on)
            # and back-of-queue contention
            # (high dispatch_ms; commit wait or reply delivery after dispatch).
            logger.warning(
                "broker slow rpc [%s] (%.1fms total = %.1fms queued + "
                "%.1fms dispatch, qsize=%d): %.80s -> ok=%s, delivery=%s",
                delivery.worker_tag,
                total_ms,
                delivery.queue_wait_ms,
                dispatch_ms,
                delivery.queue.qsize(),
                delivery.line.decode("utf-8", "replace"),
                reply.ok,
                outcome,
            )
        else:
            logger.debug(
                "broker rpc [%s]: %.80s -> ok=%s%s "
                "(%.1fms total = %.1fms queued + %.1fms dispatch, delivery=%s)",
                delivery.worker_tag,
                delivery.line.decode("utf-8", "replace"),
                reply.ok,
                f" error={reply.error}" if not reply.ok else "",
                total_ms,
                delivery.queue_wait_ms,
                dispatch_ms,
                outcome,
            )

    def _deliver_reply(
        self, reply: Reply, delivery: _ReplyDelivery
    ) -> Literal["sent", "failed"]:
        """Serialize frames and close a connection after an incomplete send."""
        payload = reply.model_dump_json().encode("utf-8") + b"\n"
        conn = delivery.conn
        with conn.send_lock:
            try:
                conn.sock.sendall(payload)
            except OSError:
                # A failed send may leave a partial JSON frame. Prevent later
                # workers from appending another response to that broken frame.
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                logger.debug(
                    "broker [%s]: dropped reply on closed or blocked sock",
                    delivery.worker_tag,
                )
                return "failed"
        return "sent"

    @staticmethod
    def _finish_request(conn: ClientConnection, q: queue.Queue) -> None:
        with conn.activity_lock:
            conn.pending -= 1
            conn.last_activity = time.monotonic()
        q.task_done()

    def start_workers(self) -> None:
        """Spawn worker threads for each queue. Call once after
        construction (before `serve_forever`).

        Each queue's worker count is operator-tunable via Settings:
        `broker_cache_workers` (default 1), `broker_long_workers`
        (default 1), `broker_warm_workers` (default 4). All three
        share the same `_worker_loop` shape: a thread doing
        `queue.get()` then dispatch then reply, in a loop. N
        workers on one queue means N parallel drains.

        Defaults reflect each queue's natural shape, not a hard
        constraint:

        - **cache**: single-worker default preserves FIFO commit
          order per submitting client (every cache.set on
          a given key from a given client lands on the WriterThread
          in submit order). Bumping to N parallelises the dispatch
          step but reorders concurrent submits at the WriterThread;
          safe for mimir's cache.set (idempotent on key,
          last-writer-wins after TTL) but callers relying on
          ordering between distinct ops would need the
          partition-by-key or submit-lock shapes.
        - **long**: long ops are write-heavy and funnel at the
          WriterThread anyway, so bumping the worker count rarely
          improves wall time. The exception is non-trivial pre-write
          compute phases (batch building, read fan-outs) that can
          overlap across workers.
        - **warm**: read-heavy compute (recursive CTEs against the
          read pool) with a small trailing write; the multi-worker
          default exploits parallel reads cleanly. Cap is the read
          pool size (`broker_read_pool_size`).

        Each worker still funnels its actual SQLite writes through
        the single WriterThread underneath."""
        assert not self._cache_worker_threads, "workers already started"
        for queue_attr, count_setting, label_prefix, tracker in (
            (
                "cache_queue",
                settings.broker_cache_workers,
                "cache",
                self._cache_worker_threads,
            ),
            (
                "long_queue",
                settings.broker_long_workers,
                "long",
                self._long_worker_threads,
            ),
            (
                "warm_queue",
                settings.broker_warm_workers,
                "warm",
                self._warm_worker_threads,
            ),
        ):
            n = max(1, count_setting)
            for i in range(n):
                label = label_prefix if n == 1 else f"{label_prefix}-{i}"
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(getattr(self, queue_attr), label),
                    daemon=True,
                    name=f"broker-{label}-worker",
                )
                t.start()
                tracker.append(t)
        self._watchdog_thread = threading.Thread(
            target=self.in_flight.run,
            args=(self.stop_event, settings.broker_stuck_job_warn_interval_sec),
            daemon=True,
            name="broker-watchdog",
        )
        self._watchdog_thread.start()


def _check_peer_uid(sock: socket.socket) -> bool:
    """Return True if the connecting peer's euid matches the broker's
    own. On platforms without `SO_PEERCRED` (macOS, BSDs) the check
    is skipped and True is returned; the socket file mode + parent-
    directory ownership remain the access control there.

    Treats a `getsockopt` failure as "allow" rather than killing the
    connection: this is defense-in-depth, not the primary gate, and
    a failing syscall is a worse signal than the missed check."""
    if not _HAS_SO_PEERCRED:
        return True
    try:
        creds = sock.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )
        pid, uid, gid = struct.unpack("3i", creds)
    except OSError as exc:
        logger.debug("broker: SO_PEERCRED unavailable: %s", exc)
        return True
    if uid != _BROKER_EUID:
        logger.warning(
            "broker: refusing connection from uid=%d pid=%d (broker euid=%d)",
            uid,
            pid,
            _BROKER_EUID,
        )
        return False
    return True


def _purge_loop(stop_event: threading.Event, writer: WriterThread) -> None:
    """Periodic purge of expired cache rows. Submitted as a WriteOp via
    the broker's `WriterThread` so every write funnels through the
    single-writer invariant; concurrent handler writes can no longer
    push this thread's DEFERRED `BEGIN` into a `SQLITE_BUSY_SNAPSHOT`
    that silently skips a purge tick (audit #475).

    Runs immediately on its background thread, then waits between attempts.
    An admitted purge stays the only pending attempt until it completes or
    shutdown leaves it to the writer's drain. Housekeeping never waits for
    queue admission or competes with an already active VACUUM.
    """
    while not stop_event.is_set():
        try:
            if not is_vacuum_active():
                future = cache.purge_expired_via_writer(writer, block=False)
                while not future.done():
                    if stop_event.wait(SHUTDOWN_POLL_SEC):
                        return
                n = future.result()
                if n:
                    logger.info("broker purge: %d expired rows deleted", n)
        except queue.Full:
            # Cleanup is retryable; do not park a producer on writer shutdown.
            logger.debug("broker purge: writer queue full, deferring cleanup")
        except Exception:
            # Periodic task; log + continue. A DB error here would
            # crash the thread otherwise and leave the broker
            # write-only until restart.
            logger.exception("broker purge: tick failed, continuing")
        if stop_event.wait(PURGE_INTERVAL_SEC):
            return


def build_server(socket_path: Path) -> _BrokerServer:
    """Bind a broker server on `socket_path` and return it (not yet
    serving). Unlinks any stale socket file, creates parent dirs,
    sets file mode 0660. Starts both worker threads (cache + long)
    so an immediate accept can hand work off without a race. Used
    by `serve()` (production entry) and by tests that need to drive
    the server without signal handling.

    Caller is responsible for `serve_forever()` and `server_close()`
    + socket-file cleanup; `serve()` does both.

    The umask dance around `_BrokerServer(...)` makes the socket
    node born `0660`. `socketserver.UnixStreamServer.server_bind`
    invokes `bind(2)`, which creates the node with permissions of
    `0o666 & ~umask`; a process umask of `0022` would leave it
    world-connectable for the window between bind and the explicit
    `chmod` below. By forcing umask to `0o117` for the bind we get
    the desired mode atomically; the trailing `os.chmod` stays in
    place so an upgrade that loses the umask still ends up with the
    right mode."""
    sp = Path(socket_path)
    if sp.exists():
        logger.info("broker: unlinking stale socket %s", sp)
        sp.unlink()
    sp.parent.mkdir(parents=True, exist_ok=True)
    prev_umask = os.umask(0o117)
    try:
        server = _BrokerServer(str(sp))
    finally:
        os.umask(prev_umask)
    os.chmod(sp, 0o660)
    server.start_workers()
    # Keep preparation before readiness while serve() has not started or
    # registered the writer. The startup module owns database step ordering.
    startup.prepare_database(sp)
    # Config-drift guard (Layer 1): the broker can serve every non-
    # sitemap surface with SITE_BASE_URL unset, but the sitemap warm
    # targets (sitemap:index, sitemap:meta, sitemap:inbox:*) silently
    # become no-ops because `_build_global_targets("")` returns an
    # empty list when the base URL is missing. Surface that the
    # moment the broker comes up so an operator reading `podman logs
    # mimir-broker` sees it, rather than discovering the cliff
    # later when cached sitemap rows age out without ever refreshing.
    # Driven by the 2026-06-01 production incident where SITE_BASE_URL
    # was set on mimir-tasks and mimir-app but missing on
    # mimir-broker. Don't fail-fast: non-sitemap surfaces still work.
    if not settings.site_base_url.strip():
        logger.warning(
            "broker startup: SITE_BASE_URL is unset; sitemap warming "
            "(sitemap:index, sitemap:meta, sitemap:inbox:*) will be a "
            "no-op because the broker can't derive the canonical base URL. "
            "Set SITE_BASE_URL in the broker container's environment."
        )
    logger.info("broker: listening on %s", sp)
    return server


def _make_signal_handler(server: _BrokerServer):
    """Build the SIGTERM/SIGINT handler used by `serve()`. Returns
    `(handler, state)` so callers (and tests) can inspect the handler's
    state without touching module-level globals.

    The handler is idempotent: a second signal arriving after the first
    has fired logs and returns rather than spawning a second
    `server.shutdown()` thread. `socketserver.BaseServer.shutdown()` is
    not documented as safe to call concurrently; a second call hangs on
    the internal one-shot Event, which would dangle the shutdown thread
    even though the broker process itself shuts down via the daemon
    flag (audit #484).

    `state["thread"]` carries the spawned shutdown thread so `serve()`'s
    finally can join it instead of relying on daemon-thread reclamation.
    """
    lock = threading.Lock()
    state: dict[str, object] = {"started": False, "thread": None}

    def _on_signal(signum, _frame):
        signame = signal.Signals(signum).name
        with lock:
            if state["started"]:
                logger.info(
                    "broker: %s received, shutdown already in progress; ignoring",
                    signame,
                )
                return
            state["started"] = True
            logger.info("broker: %s received, shutting down", signame)
            server.stop_event.set()
            # `shutdown()` blocks until `serve_forever` returns. Spawn
            # it on a fresh thread so the signal handler returns
            # promptly; the parent `serve()` joins this thread in its
            # finally.
            t = threading.Thread(
                target=server.shutdown,
                daemon=True,
                name="broker-shutdown",
            )
            state["thread"] = t
            t.start()

    return _on_signal, state


def _register_stack_dump_signal(file=None) -> None:
    """Make `SIGUSR1` dump every thread's Python stack to `file`
    (stderr, which is the broker's log) and carry on running.

    A hung worker shows up to the kernel as an empty stack in
    `futex_wait`; where it blocked in Python is invisible from outside
    the process, and a restart destroys the evidence (#1123). One
    signal before the restart records it. `faulthandler` writes from
    its C handler without taking the GIL or any Python lock, so the
    dump works even when the threads it describes are deadlocked.
    `chain=False`: nothing else in the broker handles SIGUSR1, and
    chaining to the default disposition would terminate the process."""
    faulthandler.register(
        signal.SIGUSR1,
        file=file if file is not None else sys.stderr,
        all_threads=True,
        chain=False,
    )


def serve(socket_path: Path) -> None:
    """Start the broker daemon. Blocks until SIGTERM/SIGINT.

    Wires the signal handlers (SIGTERM/SIGINT stop; SIGUSR1 dumps
    every thread's stack and continues), starts the periodic purge thread,
    runs the accept loop on this thread. Tests should NOT call this;
    they use `build_server()` + manual control instead, since signal
    handlers only register from the main thread."""
    # First, before build_server: its migration and backfills can run
    # for minutes, and a stack dump of a startup that hangs there is
    # as useful as one of a hung worker. Registered later, SIGUSR1
    # would hit its default action and kill the broker instead.
    _register_stack_dump_signal()
    server = build_server(socket_path)
    sp = Path(socket_path)

    server.writer.start()
    # Phase 2 of the two-pool restructure registers this server's
    # pool + writer so handler functions (warm.py, ingest, etc.) can reach
    # them without a dispatcher refactor.
    _context.set_active(server.read_pool, server.writer)

    purge_thread = threading.Thread(
        target=_purge_loop,
        args=(server.stop_event, server.writer),
        daemon=True,
        name="broker-purge",
    )
    purge_thread.start()

    signal_handler, shutdown_state = _make_signal_handler(server)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    tracemalloc_thread = _maybe_start_tracemalloc_snapshotter(
        settings.tracemalloc_interval_seconds,
        stop_event=server.stop_event,
    )

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()
        # Idle workers notice stop_event on their next poll; in-flight work
        # may outlive these bounded joins. Give replies a chance to land,
        # then report an incomplete cache drain rather than promise delivery.
        for t in (
            *server._cache_worker_threads,
            *server._long_worker_threads,
            *server._warm_worker_threads,
        ):
            t.join(timeout=5.0)
        cache_workers_alive = sum(t.is_alive() for t in server._cache_worker_threads)
        incomplete_cache_drain = (
            cache_workers_alive or server.cache_queue.unfinished_tasks
        )
        if incomplete_cache_drain:
            logger.warning(
                "broker: incomplete cache reply drain: "
                "cache_workers_alive=%d, cache_pending=%d",
                cache_workers_alive,
                server.cache_queue.unfinished_tasks,
            )
        purge_thread.join(timeout=5.0)
        if server._watchdog_thread is not None:
            server._watchdog_thread.join(timeout=5.0)
        if tracemalloc_thread is not None:
            tracemalloc_thread.join(timeout=5.0)
        # Join the signal-spawned shutdown thread so a fast SIGTERM
        # right before process exit doesn't leave it dangling (audit
        # #484). The thread is already done by the time we get here
        # (its `server.shutdown()` returned the moment `serve_forever`
        # exited above), so the join is effectively instant.
        shutdown_thread = shutdown_state.get("thread")
        if shutdown_thread is not None:
            shutdown_thread.join(timeout=5.0)
        server.writer.stop(timeout=10.0)
        _context.clear_active()
        server.read_pool.close()
        if not incomplete_cache_drain:
            logger.info("broker: shut down cleanly")


def _log_tracemalloc_top_25(snap: tracemalloc.Snapshot) -> None:
    """Log the top-25 allocation sites by current bytes at INFO.

    One header line plus one entry per site, formatted as
    '<bytes_mib>  <file>:<lineno>' so an operator following along
    via `podman logs` sees the leak shape forming.
    """
    stats = snap.statistics("lineno")[:25]
    logger.info("broker: tracemalloc top-25 by current bytes:")
    for stat in stats:
        # stat.traceback is sorted oldest -> most recent; [-1] is the
        # allocation site, which is what an operator looking for the
        # leak wants to see (not the outer thread/event-loop frame).
        frame = stat.traceback[-1]
        size_mib = stat.size / (1024 * 1024)
        logger.info("  %7.1f MiB  %s:%d", size_mib, frame.filename, frame.lineno)


def _maybe_start_tracemalloc_snapshotter(
    interval: int,
    stop_event: threading.Event,
    diagnostics_dir: Path = Path("/data/diagnostics"),
    frames: int = 25,
) -> threading.Thread | None:
    """Start tracemalloc + a snapshotter thread when interval > 0.

    Returns the spawned thread so `serve()` can join it on a clean
    shutdown; returns None when disabled (interval <= 0) or when
    startup fails (e.g., diagnostics_dir not writable).

    `stop_event` is the broker's shared stop signal. The snapshotter
    waits on it between iterations and exits as soon as it fires,
    so a clean shutdown breaks out of the loop instead of risking a
    mid-`pickle.dump` process exit that would leave `.pkl.tmp` files
    orphaned under `diagnostics_dir` (audit #477).

    See _claude/specs/2026-06-11-broker-tracemalloc-diagnostic-design.md.
    """
    if interval <= 0:
        return None
    try:
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("broker: tracemalloc snapshotter disabled: %s", e)
        return None
    tracemalloc.start(frames)
    logger.info(
        "broker: tracemalloc enabled (interval=%ds, frames=%d); snapshots -> %s",
        interval,
        frames,
        diagnostics_dir,
    )

    def _loop() -> None:
        while True:
            try:
                snap = tracemalloc.take_snapshot()
                ts = datetime.now(UTC).isoformat(timespec="seconds")
                path = diagnostics_dir / f"tracemalloc-{ts}.pkl"
                tmp = path.with_suffix(".pkl.tmp")
                with open(tmp, "wb") as f:
                    pickle.dump(snap, f)
                os.rename(tmp, path)
                _log_tracemalloc_top_25(snap)
            except Exception as e:
                logger.warning("broker: tracemalloc snapshot failed: %s", e)
            # `wait()` returns True when the event is set; False on
            # timeout. Stopping here (rather than `time.sleep`) lets
            # `serve()` join this thread on shutdown.
            if stop_event.wait(interval):
                tracemalloc.stop()
                return

    thread = threading.Thread(
        target=_loop,
        name="broker-tracemalloc",
        daemon=True,
    )
    thread.start()
    return thread


__all__ = ["PURGE_INTERVAL_SEC", "ClientConnection", "Reply", "build_server", "serve"]
