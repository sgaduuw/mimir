"""Broker daemon lifecycle: UNIX socket accept loop + signal
handling + periodic-purge thread.

Single-threaded SocketServer (`UnixStreamServer`, no
`ThreadingMixIn`): every request is processed in sequence on one
thread, which serialises writes in-process by construction. We
don't need a Python-level lock around the writer connection;
serial-by-handler is the lock. Latency cost is negligible (each
RPC commits in ~ms on the pooled connection).

The periodic-purge thread runs alongside, calling
`cache._direct_purge_expired` on a configurable cadence. The
broker owns the cache table's data lifecycle end-to-end (writes +
purge); the scheduler sidecar's `warm-cache` no longer drives
purges when broker mode is on.

Signal handler (`SIGTERM`, `SIGINT`) sets a stop flag, the accept
loop exits, the socket file is unlinked, the purge thread joins.
"""
import logging
import os
import selectors
import signal
import socketserver
import threading
from pathlib import Path

from mimir import cache
from mimir.broker.handlers import dispatch
from mimir.broker.protocol import Reply

logger = logging.getLogger(__name__)


PURGE_INTERVAL_SEC = 3600  # 1 hour; matches today's warm-cache-driven cadence


class _ConnectionHandler(socketserver.StreamRequestHandler):
    """One per accepted connection. Reads JSONL request lines from
    the client, writes JSONL reply lines back, until the client
    closes the socket (or the broker is shutting down).

    Uses `selectors.select` to poll for readable data so a quiet
    persistent client (gunicorn worker between cache.set calls)
    doesn't pin this handler thread blocked indefinitely. On
    timeout, we loop back to re-check `stop_event`. Without this,
    SIGTERM during a quiet window would never reach this thread and
    the broker process couldn't exit cleanly.

    The earlier shape used `self.request.settimeout(0.1)` + buffered
    `rfile.readline()`. That trips a Python stdlib gotcha: after one
    `socket.timeout`, `socket.makefile`'s `SocketIO` flags
    `_timeout_occurred=True` and every subsequent read raises
    `OSError("cannot read from timed out object")`, permanently
    poisoning the rfile. Production logs filled with that exception
    on every idle window > 100ms (typical between cache.set calls
    from a single web worker). Switched to raw `socket.recv` with a
    selector-based wait, accumulating into a line buffer ourselves
    so framing stays JSONL but the socket doesn't enter the broken
    timed-out state.
    """

    def setup(self) -> None:
        super().setup()
        self._selector = selectors.DefaultSelector()
        self._selector.register(self.request, selectors.EVENT_READ)
        self._linebuf = bytearray()

    def finish(self) -> None:
        try:
            self._selector.close()
        except Exception:
            pass
        super().finish()

    def _drain_lines(self) -> list[bytes]:
        """Pull complete `\\n`-delimited lines out of the accumulator
        and return them. Trailing partial line (no newline yet) stays
        in `_linebuf` for the next read pass."""
        out: list[bytes] = []
        while True:
            nl = self._linebuf.find(b"\n")
            if nl < 0:
                break
            out.append(bytes(self._linebuf[:nl]))
            del self._linebuf[:nl + 1]
        return out

    def handle(self) -> None:
        while not self.server.stop_event.is_set():
            events = self._selector.select(timeout=0.1)
            if not events:
                # Idle window; re-check stop_event and loop.
                continue
            try:
                chunk = self.request.recv(4096)
            except (OSError, ConnectionError):
                # Peer reset / EBADF / etc. Treat as a normal close.
                return
            if not chunk:
                # Clean EOF from the peer.
                return
            self._linebuf.extend(chunk)
            for line in self._drain_lines():
                reply = dispatch(line)
                # Per-RPC DEBUG visibility: log the leading bytes of
                # the request line (enough to see the op + key) plus
                # the reply's ok flag. Default INFO floor keeps this
                # off; pass `-v` on `mimir broker` to enable.
                logger.debug(
                    "broker rpc: %.80s -> ok=%s%s",
                    line.decode("utf-8", "replace"),
                    reply.ok,
                    f" error={reply.error}" if not reply.ok else "",
                )
                self.wfile.write(reply.model_dump_json().encode("utf-8"))
                self.wfile.write(b"\n")
                self.wfile.flush()


class _BrokerServer(socketserver.UnixStreamServer):
    """UnixStreamServer with a stop-event the request handler polls
    so a graceful shutdown reaches in-flight connections. `allow_reuse_address`
    isn't meaningful for AF_UNIX, we unlink the stale socket file
    before binding instead."""

    daemon_threads = True

    def __init__(self, socket_path: str) -> None:
        super().__init__(socket_path, _ConnectionHandler)
        self.stop_event = threading.Event()


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


def build_server(socket_path: Path) -> "_BrokerServer":
    """Bind a broker server on `socket_path` and return it (not yet
    serving). Unlinks any stale socket file, creates parent dirs,
    sets file mode 0660. Used by `serve()` (production entry) and
    by tests that need to drive the server without signal handling.

    Caller is responsible for `serve_forever()` and `server_close()` +
    socket-file cleanup; `serve()` does both. Tests typically run
    `serve_forever` in a thread and call `shutdown()` + cleanup
    manually."""
    sp = Path(socket_path)
    if sp.exists():
        logger.info("broker: unlinking stale socket %s", sp)
        sp.unlink()
    sp.parent.mkdir(parents=True, exist_ok=True)
    server = _BrokerServer(str(sp))
    os.chmod(sp, 0o660)
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
