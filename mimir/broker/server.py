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
import threading
import time
from pathlib import Path

from mimir import cache
from mimir.broker.handlers import LONG_OPS, WARM_OPS, classify_op, dispatch
from mimir.broker.protocol import Reply
from mimir.config import settings

logger = logging.getLogger(__name__)


PURGE_INTERVAL_SEC = 3600  # 1 hour; matches today's warm-cache-driven cadence

# How often the worker / reader threads check `stop_event` while idle.
# Bounds the shutdown latency from SIGTERM to clean exit.
SHUTDOWN_POLL_SEC = 0.1


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

    def process_request(self, request, client_address) -> None:
        """Override of `BaseServer.process_request` so each accepted
        connection runs on its own thread instead of synchronously
        on the accept loop. The reader thread owns the socket
        lifecycle (closes it on EOF / error / shutdown); we do NOT
        call `shutdown_request` here for that reason."""
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
        failure reply, preserving existing error semantics."""
        linebuf = bytearray()
        sel = selectors.DefaultSelector()
        sel.register(sock, selectors.EVENT_READ)
        try:
            while not self.stop_event.is_set():
                events = sel.select(timeout=SHUTDOWN_POLL_SEC)
                if not events:
                    continue
                try:
                    chunk = sock.recv(4096)
                except (OSError, ConnectionError):
                    return
                if not chunk:
                    return  # Clean EOF from peer.
                linebuf.extend(chunk)
                while True:
                    nl = linebuf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(linebuf[:nl])
                    del linebuf[:nl + 1]
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
                reply = dispatch(line)
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
                        total_ms, queue_wait_ms, dispatch_ms,
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
                        total_ms, queue_wait_ms, dispatch_ms,
                    )

                payload = reply.model_dump_json().encode("utf-8") + b"\n"
                try:
                    sock.sendall(payload)
                except (OSError, ConnectionError):
                    # Client closed mid-flight or socket reset.
                    # Drop the reply silently; the client treats
                    # the missing reply as `BrokerUnavailable` and
                    # retries.
                    logger.debug("broker [%s]: dropped reply on closed sock", worker_tag)
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
    + socket-file cleanup; `serve()` does both."""
    sp = Path(socket_path)
    if sp.exists():
        logger.info("broker: unlinking stale socket %s", sp)
        sp.unlink()
    sp.parent.mkdir(parents=True, exist_ok=True)
    server = _BrokerServer(str(sp))
    os.chmod(sp, 0o660)
    server.start_workers()
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

    purge_thread = threading.Thread(
        target=_purge_loop, args=(server.stop_event,), daemon=True,
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
        logger.info("broker: shut down cleanly")


__all__ = ["build_server", "serve", "Reply", "PURGE_INTERVAL_SEC"]
