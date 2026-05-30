"""Broker daemon: queue + worker-pool architecture.

The broker accepts an arbitrary number of concurrent client
connections. Each connection runs a small **reader thread** that
pulls JSONL request lines off the socket and enqueues them onto a
shared work queue. A single **worker thread** dequeues, dispatches
the RPC against the SQLite writer, and writes the reply back to
the originating connection.

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

2. **Writes stay serialised at one worker.** A single worker
   thread dequeues + dispatches + replies, so two concurrent
   client RPCs never race for the SQLAlchemy connection pool's
   writer slot or hit unnecessary `SQLITE_BUSY` retries from
   in-process contention. The serialisation that used to be a
   property of `serve_forever`'s synchronous handler is now a
   property of the single-worker queue drain.

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
   throughput needs it. Phase 1.5+ work.

The periodic-purge thread is unchanged: calls
`cache._direct_purge_expired` on `PURGE_INTERVAL_SEC` cadence.

Signal handlers (`SIGTERM`, `SIGINT`) set `stop_event`; reader
threads notice via their selector poll, the worker notices via
its `queue.get(timeout=...)` poll, all exit cleanly.
"""

import logging
import os
import queue
import selectors
import signal
import socket
import socketserver
import struct
import threading
import time
from pathlib import Path

from mimir import cache
from mimir.broker.handlers import LONG_OPS, WARM_OPS, classify_op, dispatch
from mimir.broker.pools import ReadSessionPool
from mimir.broker.protocol import Reply
from mimir.broker.writes import WriterThread
from mimir.config import settings

logger = logging.getLogger(__name__)


PURGE_INTERVAL_SEC = 3600  # 1 hour; matches today's warm-cache-driven cadence

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
# Legitimate persistent clients (gunicorn workers, scheduler-tasks)
# send keepalive pings well inside the window; broker_ping CLI is
# one-shot.
IDLE_TIMEOUT_SEC = 300

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


class _NoopHandler(socketserver.BaseRequestHandler):
    """Stub passed to `socketserver` because the framework requires a
    RequestHandlerClass argument. `_BrokerServer.process_request`
    overrides the per-connection handler dispatch to spawn a reader
    thread instead, so this class is never instantiated in
    practice."""

    def handle(self) -> None:
        raise RuntimeError(
            "broker: _NoopHandler.handle should not be reached; "
            "_BrokerServer.process_request overrides this path"
        )


class _BrokerServer(socketserver.UnixStreamServer):
    """UNIX-socket server that hands every accepted connection off
    to a dedicated reader thread and runs **three** classes of
    worker thread draining three separate work queues:

    - `cache_queue` for cache ops (sub-ms commits). Always-on
      throughput; the only thing the web tier waits on. **One**
      worker thread (write ordering matters here).
    - `long_queue` for long-running ops (`bootstrap_inboxes` in
      Phase 2.0; ingest / backfills / mainline / analyze / vacuum
      in Phase 2.1+). **One** worker thread; one op at a time can
      run for minutes.
    - `warm_queue` for cache warming (Phase 2.2). **N** worker
      threads (default 4, env `BROKER_WARM_WORKERS`) so the
      compute phase of warming overlaps across inboxes; cache.set
      commits still funnel through the SQLite writer lock but
      every warm worker has its own session for the read phase.

    The cache + long workers contend for the SQLite writer lock at
    the SQLite level (via `busy_timeout`); cache writes never wait
    behind the *whole* long op, just behind the long op's current
    commit batch. The warm workers add another N entries to that
    contention pool, but each warm cache.set is short-lived, so
    they queue cleanly behind any cache op the web tier is firing.

    `daemon_threads = True` so reader / worker threads don't block
    process exit on unclean shutdown.

    `request_queue_size` (the kernel's `listen()` backlog) stays at
    256 (from 1.32.3) so brief bursts above the accept rate don't
    surface as EAGAIN at the client.
    """

    request_queue_size = 256

    def __init__(self, socket_path: str) -> None:
        super().__init__(socket_path, _NoopHandler)
        self.stop_event = threading.Event()
        # Three queues, one per worker class. Items are
        # `(line, sock, enqueued_at)` tuples; the reader classifies
        # by op name and routes (`handlers.classify_op` →
        # `handlers.LONG_OPS` / `WARM_OPS`). Unbounded queues:
        # under observed peak load (a few hundred cache.sets per
        # warm-cache tick) memory is negligible and a hard cap
        # would just drop work silently. The slow-RPC WARNING
        # (with breakdown into queue vs dispatch) is the operator-
        # facing signal.
        self.cache_queue: "queue.Queue[tuple[bytes, socket.socket, float]]" = (
            queue.Queue()
        )
        self.long_queue: "queue.Queue[tuple[bytes, socket.socket, float]]" = (
            queue.Queue()
        )
        self.warm_queue: "queue.Queue[tuple[bytes, socket.socket, float]]" = (
            queue.Queue()
        )
        self._reader_threads: list[threading.Thread] = []
        self._cache_worker_thread: threading.Thread | None = None
        self._long_worker_thread: threading.Thread | None = None
        self._warm_worker_threads: list[threading.Thread] = []
        # Phase 1 of the two-pool restructure
        # (_claude/specs/2026-05-29-broker-two-pool-design.md): parallel
        # infrastructure, no handler uses these yet. Future Phase 2+
        # patches migrate handlers to dispatch reads through
        # `self.read_pool.session()` and writes via
        # `self.writer.submit(op)`. Owned by the server so lifecycle
        # ordering (start before accept, stop after drain) is
        # explicit.
        self.read_pool = ReadSessionPool.from_settings()
        self.writer = WriterThread.from_settings()

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
          accepts a slot and then sends nothing would otherwise pin
          this reader thread + its selector indefinitely. Beyond the
          ceiling the connection is closed.

        Both defenses log at WARNING so an operator can correlate the
        close with the upstream symptom (a stuck `cache.set` etc.)."""
        linebuf = bytearray()
        sel = selectors.DefaultSelector()
        sel.register(sock, selectors.EVENT_READ)
        last_activity = time.monotonic()
        try:
            while not self.stop_event.is_set():
                events = sel.select(timeout=SHUTDOWN_POLL_SEC)
                if not events:
                    if time.monotonic() - last_activity > IDLE_TIMEOUT_SEC:
                        logger.warning(
                            "broker: closing idle connection after %ds",
                            IDLE_TIMEOUT_SEC,
                        )
                        return
                    continue
                try:
                    chunk = sock.recv(4096)
                except OSError, ConnectionError:
                    return
                if not chunk:
                    return  # Clean EOF from peer.
                last_activity = time.monotonic()
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
                    if op is not None and op in LONG_OPS:
                        target = self.long_queue
                    elif op is not None and op in WARM_OPS:
                        target = self.warm_queue
                    else:
                        target = self.cache_queue
                    target.put((line, sock, time.perf_counter()))
        finally:
            sel.close()
            try:
                sock.close()
            except OSError:
                pass

    def _worker_loop(
        self,
        q: "queue.Queue[tuple[bytes, socket.socket, float]]",
        worker_tag: str,
    ) -> None:
        """Drain one queue serially. One RPC at a time on this
        worker so writes stay ordered at the SQLAlchemy layer
        without an extra lock. Each iteration: dequeue, dispatch,
        write reply to the originating socket. Slow-RPC WARNING
        fires with queue-wait + dispatch breakdown.

        `worker_tag` tags the slow-RPC log line ("cache" or "long")
        so operators reading the broker log can tell which queue
        is contended without inferring it from the op string.
        """
        while not self.stop_event.is_set():
            try:
                line, sock, enqueued_at = q.get(timeout=SHUTDOWN_POLL_SEC)
            except queue.Empty:
                continue
            try:
                queue_wait_ms = (time.perf_counter() - enqueued_at) * 1000.0
                t0 = time.perf_counter()
                # Mark this worker thread as "currently dispatching a
                # broker handler" so any reachable `cache.set` /
                # `cache.delete_for_inbox` etc. falls through to the
                # direct write path rather than attempting a self-RPC
                # back through the socket. Production handles the
                # same concern via `MIMIR_IS_BROKER=true` env at the
                # process level, but the in-process test environment
                # shares `settings.mimir_is_broker` between broker
                # thread and CLI invocations; the thread-local flag
                # is the right granularity in either case.
                cache._set_broker_handler_active(True)
                try:
                    reply = dispatch(line)
                finally:
                    cache._set_broker_handler_active(False)
                dispatch_ms = (time.perf_counter() - t0) * 1000.0
                total_ms = queue_wait_ms + dispatch_ms

                threshold_ms = settings.broker_slow_rpc_warn_ms
                if threshold_ms > 0 and total_ms >= threshold_ms:
                    # Breakdown helps operators tell the
                    # difference between front-of-queue contention
                    # (high queue_wait_ms; many clients piling on)
                    # and back-of-queue contention
                    # (high dispatch_ms; SQLite writer lock held
                    # by the other worker or, in Phase 1 deploys,
                    # by direct scheduler-side writes).
                    logger.warning(
                        "broker slow rpc [%s] (%.1fms total = %.1fms queued + "
                        "%.1fms dispatch, qsize=%d): %.80s -> ok=%s",
                        worker_tag,
                        total_ms,
                        queue_wait_ms,
                        dispatch_ms,
                        q.qsize(),
                        line.decode("utf-8", "replace"),
                        reply.ok,
                    )
                else:
                    logger.debug(
                        "broker rpc [%s]: %.80s -> ok=%s%s "
                        "(%.1fms total = %.1fms queued + %.1fms dispatch)",
                        worker_tag,
                        line.decode("utf-8", "replace"),
                        reply.ok,
                        f" error={reply.error}" if not reply.ok else "",
                        total_ms,
                        queue_wait_ms,
                        dispatch_ms,
                    )

                payload = reply.model_dump_json().encode("utf-8") + b"\n"
                try:
                    sock.sendall(payload)
                except OSError, ConnectionError:
                    # Client closed mid-flight or socket reset.
                    # Drop the reply silently; the client treats
                    # the missing reply as `BrokerUnavailable` and
                    # retries.
                    logger.debug(
                        "broker [%s]: dropped reply on closed sock", worker_tag
                    )
            finally:
                q.task_done()

    def start_workers(self) -> None:
        """Spawn worker threads for each queue. Call once after
        construction (before `serve_forever`).

        Cache and long queues get one worker each (write ordering).
        Warm queue gets `settings.broker_warm_workers` workers
        (default 4) so the read-heavy compute phase of warming
        parallelises across inboxes; cache.set commits still
        serialise at the SQLite writer lock, but the upstream
        compute overlaps."""
        assert self._cache_worker_thread is None, "workers already started"
        self._cache_worker_thread = threading.Thread(
            target=self._worker_loop,
            args=(self.cache_queue, "cache"),
            daemon=True,
            name="broker-cache-worker",
        )
        self._long_worker_thread = threading.Thread(
            target=self._worker_loop,
            args=(self.long_queue, "long"),
            daemon=True,
            name="broker-long-worker",
        )
        self._cache_worker_thread.start()
        self._long_worker_thread.start()
        # Warm workers: N parallel drains of the same queue. One op
        # at a time per worker; concurrency across workers.
        warm_n = max(1, settings.broker_warm_workers)
        for i in range(warm_n):
            t = threading.Thread(
                target=self._worker_loop,
                args=(self.warm_queue, f"warm-{i}"),
                daemon=True,
                name=f"broker-warm-worker-{i}",
            )
            t.start()
            self._warm_worker_threads.append(t)


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


def _migrate_if_needed(socket_path: Path) -> None:
    """Run `alembic upgrade head` on broker startup so the broker
    can serve against a schema known to be current.

    `alembic upgrade head` is idempotent and cheap when no
    migrations are pending (one SELECT against `alembic_version`),
    so we always run it. Pre-2.4.1 used `.migrated` as a "ran once,
    skip forever" gate, which silently swallowed every new alembic
    revision on long-lived deploys; 2.4.0 introduced
    `mainline_state.last_walked_at` and the broker on an upgraded
    deploy skipped it because the sentinel was present from 2.3.0,
    so `update_mainline()` raised `OperationalError: no such
    column`. The sentinel survives as a first-vs-subsequent-run
    marker for operator log-reading, but its presence no longer
    short-circuits the upgrade call.

    2.0.0 moved schema-migration ownership onto the broker because
    it's a write operation and broker is the sole writer process
    post-cleanup. The pre-2.0.0 shape ran `alembic upgrade head` on
    the tasks container as a startup-only direct write; consolidating
    here matches the single-writer invariant without exception."""
    sentinel = socket_path.parent / ".migrated"
    first_run = not sentinel.exists()
    logger.info(
        "broker: running alembic upgrade head (%s)",
        "first run" if first_run else "idempotent re-check",
    )
    t0 = time.monotonic()
    try:
        from alembic import command
        from alembic.config import Config

        # Construct Config programmatically rather than passing
        # "alembic.ini": that ini carries [loggers] / [handlers] /
        # [formatters] sections, and Config reading them invokes
        # `logging.config.fileConfig` which clears every handler off
        # the root logger, including pytest's caplog and any
        # operator-set handlers. Loading the migration's required
        # options directly side-steps the reconfiguration. The
        # migrations live in `alembic/` per the project layout.
        cfg = Config()
        cfg.set_main_option("script_location", "alembic")
        cfg.set_main_option("sqlalchemy.url", settings.database_url)
        command.upgrade(cfg, "head")
    except Exception:
        # Refuse to start: a migration failure is load-bearing for
        # everything downstream. The healthcheck stays red so web /
        # tasks containers wait for an operator fix.
        logger.exception("broker: alembic upgrade failed, refusing to start")
        raise
    elapsed = time.monotonic() - t0
    sentinel.touch()
    logger.info(
        "broker: alembic upgrade head complete in %.1fs; sentinel %s touched",
        elapsed,
        sentinel,
    )


def _bootstrap_inboxes_if_needed(socket_path: Path) -> None:
    """Reconcile `Settings.inboxes` (env config) into the `inboxes`
    table on broker startup. Idempotent via `ON CONFLICT (name) DO
    NOTHING`; admin edits to existing rows are never clobbered.

    Sentinel-gated. The post-2.0.0 broker container owns this work
    because it's a write; pre-2.0.0 the tasks container's
    `mimir bootstrap-inboxes` did it as a startup-only direct write."""
    sentinel = socket_path.parent / ".bootstrapped"
    if sentinel.exists():
        logger.debug(
            "broker: inbox-bootstrap sentinel %s present, skipping",
            sentinel,
        )
        return
    logger.info("broker: running bootstrap_inboxes")
    t0 = time.monotonic()
    try:
        from mimir.inboxes import bootstrap_inboxes

        result = bootstrap_inboxes()
    except Exception:
        # Same posture as `_migrate_if_needed`: bootstrap is
        # load-bearing for the web tier's first request, refuse to
        # advertise readiness without it.
        logger.exception("broker: bootstrap_inboxes failed, refusing to start")
        raise
    elapsed = time.monotonic() - t0
    sentinel.touch()
    logger.info(
        "broker: bootstrap_inboxes reconciled %d inbox(es) in %.1fs; sentinel %s touched",
        len(result),
        elapsed,
        sentinel,
    )


def _post_migrate_analyze_if_needed(socket_path: Path) -> None:
    """Run `ANALYZE` once after a fresh schema migration so the
    planner has stats for any newly-created index. Bounded by the
    per-connection `analysis_limit=4000` pragma (~1-3 s on the
    production 11M-row corpus); the weekly full `ANALYZE` catches
    whatever drifts in the long tail.

    Gated on a sentinel file next to the broker socket. Once the
    first deploy has run the pass, subsequent broker restarts skip
    it. To force a re-run (e.g. after a manual schema change),
    delete the sentinel."""
    sentinel = socket_path.parent / ".broker_initial_analyze"
    if sentinel.exists():
        logger.debug(
            "broker: post-migrate ANALYZE sentinel %s present, skipping",
            sentinel,
        )
        return
    logger.info("broker: running post-migrate ANALYZE (bounded)")
    t0 = time.monotonic()
    try:
        from mimir.maintenance import run_analyze

        result = run_analyze(full=False)
    except Exception:
        # Don't refuse to start the broker if ANALYZE fails; log
        # loudly and continue. The next scheduled bounded ANALYZE
        # on the broker's own cadence retries.
        logger.exception(
            "broker: post-migrate ANALYZE failed, continuing without sentinel"
        )
        return
    elapsed = time.monotonic() - t0
    sentinel.touch()
    logger.info(
        "broker: post-migrate ANALYZE complete in %.1fs "
        "(broker-reported elapsed_ms=%d); sentinel %s touched",
        elapsed,
        result.elapsed_ms,
        sentinel,
    )


def _purge_loop(stop_event: threading.Event) -> None:
    """Periodic purge of expired cache rows. Owned by the broker so
    no other writer competes for the same table. Backs off via
    `stop_event.wait` so shutdown is responsive."""
    while not stop_event.wait(PURGE_INTERVAL_SEC):
        try:
            n = cache._direct_purge_expired()
            if n:
                logger.info("broker purge: %d expired rows deleted", n)
        except Exception:
            # Periodic task; log + continue. A DB error here would
            # crash the thread otherwise and leave the broker
            # write-only until restart.
            logger.exception("broker purge: tick failed, continuing")


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
    # Self-bootstrap sequence, in order, BEFORE we log "listening":
    #
    #   1. Alembic schema migration: schema must be at HEAD before
    #      anything else writes.
    #   2. Bootstrap inboxes: reconcile env config into the
    #      `inboxes` table so the web tier sees its expected rows
    #      on the first request.
    #   3. Post-migrate ANALYZE: a fresh schema migration may have
    #      added indexes whose `sqlite_stat1` needs populating
    #      before the planner sees them; the bounded pass takes
    #      1-3 s on the production corpus.
    #
    # Each is sentinel-gated. The web tier gates on the broker's
    # healthcheck so cold requests after deploy never hit any of
    # these in-progress. Lives in `build_server` (not `serve`) so
    # the test helper exercises the same path.
    _migrate_if_needed(sp)
    _bootstrap_inboxes_if_needed(sp)
    _post_migrate_analyze_if_needed(sp)
    logger.info("broker: listening on %s", sp)
    return server


def serve(socket_path: Path) -> None:
    """Start the broker daemon. Blocks until SIGTERM/SIGINT.

    Wires the signal handlers, starts the periodic purge thread,
    runs the accept loop on this thread. Tests should NOT call this;
    they use `build_server()` + manual control instead, since signal
    handlers only register from the main thread."""
    server = build_server(socket_path)
    sp = Path(socket_path)

    server.writer.start()

    purge_thread = threading.Thread(
        target=_purge_loop,
        args=(server.stop_event,),
        daemon=True,
        name="broker-purge",
    )
    purge_thread.start()

    def _on_signal(signum, frame):
        logger.info("broker: %s received, shutting down", signal.Signals(signum).name)
        server.stop_event.set()
        # `shutdown()` blocks until `serve_forever` returns. Call
        # from a fresh thread so signal handler returns promptly.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()
        purge_thread.join(timeout=5.0)
        server.writer.stop(timeout=10.0)
        server.read_pool.close()
        logger.info("broker: shut down cleanly")


__all__ = ["build_server", "serve", "Reply", "PURGE_INTERVAL_SEC"]
