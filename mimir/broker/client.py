"""Broker client: talk to the broker daemon from another process.

Process-singleton (`get_broker_client()`): each gunicorn worker /
each CLI invocation reuses one persistent socket connection.

Pipelined RPCs (3.0.0): multiple caller threads share one socket;
each request carries a monotonic per-client rpc_id; a daemon demux
thread reads replies and resolves matching `concurrent.futures.Future`
instances by rpc_id. See `BrokerClient` for the full design.

Reconnect: on socket failure during send, the caller's RPC raises
`BrokerUnavailable` immediately; on demux failure (EOF / OSError)
every in-flight future receives `BrokerUnavailable`. The next caller
`_rpc` call lazy-reconnects via `_ensure_alive`. No automatic retry
across reconnect; callers handle retry at their layer.

Per-RPC `timeout` lives on the caller's `Future.result(timeout=...)`;
the socket has no read timeout from the caller's perspective."""

import logging
import socket
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path

from mimir.broker.protocol import Reply, _BrokerRequest
from mimir.config import settings

logger = logging.getLogger(__name__)


class BrokerUnavailable(Exception):
    """Raised when the broker socket can't be reached or an
    in-flight RPC fails (send error, demux EOF, caller timeout,
    or client.close()). Callers in `mimir.cache` catch this and
    log+drop, matching the best-effort `OperationalError`
    semantics."""


class BrokerClient:
    """Persistent connection to the broker daemon. One per
    process; obtain via `get_broker_client()`.

    Pipelined RPCs (3.0.0): multiple caller threads can have
    in-flight requests on the same socket. Each request carries a
    monotonic per-client `rpc_id`; the broker echoes it back in the
    matching Reply; a daemon `_demux_thread` reads replies in a
    loop and resolves the matching `concurrent.futures.Future` from
    `_pending`. Caller threads block on `future.result(timeout=...)`.

    Locks:

    - `_id_lock`: protects the monotonic counter.
    - `_pending_lock`: protects the pending-futures dict.
    - `_send_lock`: held only across one `sock.sendall(...)` call
      (microseconds) so concurrent caller threads do not interleave
      bytes on the wire.
    - `_state_lock`: serialises connect / close / demux-thread
      lifecycle transitions."""

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = Path(socket_path)
        self._sock: socket.socket | None = None
        self._rfile = None

        # Monotonic per-client RPC id allocator. Client allocates
        # from 1; the broker uses 0 as a sentinel for malformed
        # requests (no matching pending future, silently dropped).
        self._next_rpc_id: int = 1
        self._id_lock = threading.Lock()

        # In-flight RPCs awaiting replies. Populated under
        # _pending_lock by the caller; popped under the same lock
        # by the demux thread.
        self._pending: dict[int, Future] = {}
        self._pending_lock = threading.Lock()

        # Held narrowly around `sock.sendall(req + b"\n")` so
        # concurrent caller threads do not tear the JSONL frame.
        self._send_lock = threading.Lock()

        # Serialises lifecycle transitions: connect, close,
        # starting/stopping the demux thread.
        self._state_lock = threading.Lock()

        self._demux_thread: threading.Thread | None = None
        self._closed: bool = False

    def _connect_locked(self) -> None:
        """Open the UNIX socket. Caller must hold `_state_lock`.
        Raises BrokerUnavailable on connect failure. Does NOT start
        the demux thread; that's `_ensure_alive`'s job."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # No SO_RCVTIMEO: the demux thread does blocking readline;
        # per-RPC deadlines live entirely on the caller's future.
        try:
            s.connect(str(self._socket_path))
        except OSError as exc:
            s.close()
            raise BrokerUnavailable(
                f"broker connect failed: {self._socket_path}: {exc}"
            ) from exc
        self._sock = s
        self._rfile = s.makefile("rb", buffering=0)

    def _close_socket(self) -> None:
        """Tear down the socket and file wrappers. Idempotent and
        safe to call concurrently with `_demux_loop` (each branch
        guards against None)."""
        if self._rfile is not None:
            try:
                self._rfile.close()
            except OSError:
                pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._rfile = None

    def _ensure_alive(self) -> None:
        """Make sure the socket is open and the demux thread is
        running. Called at the top of `_rpc`; idempotent.

        Raises BrokerUnavailable if the connect fails or the client
        is already closed."""
        with self._state_lock:
            if self._closed:
                raise BrokerUnavailable("client closed")
            if self._sock is None:
                self._connect_locked()
            if self._demux_thread is None or not self._demux_thread.is_alive():
                if self._demux_thread is not None:
                    # Wait for the old demux thread to fully exit before
                    # spawning its replacement. `is_alive()` can flip to
                    # False before the thread's `finally` clause has run
                    # to completion; without the explicit join, two demux
                    # threads could briefly read from the same `_rfile`
                    # and garble reply parsing under free-threaded
                    # Python (audit #485). 100 ms is generous: the
                    # finally clause only does `_fail_all_pending` +
                    # `_close_socket`, both microseconds.
                    self._demux_thread.join(timeout=0.1)
                t = threading.Thread(
                    target=self._demux_loop,
                    daemon=True,
                    name=f"broker-client-demux-{self._socket_path.name}",
                )
                self._demux_thread = t
                t.start()

    def _demux_loop(self) -> None:
        """Read JSONL replies in a loop; resolve the matching
        pending future. On EOF, OSError, or a file closed under it,
        fails every pending
        future with BrokerUnavailable and exits so the next caller
        reconnects fresh.

        Garbage on the wire (a Reply that fails pydantic validation)
        is treated as fatal: continuing would leak futures whose
        rpc_id we cannot determine. Log error and exit."""
        # Pin a local ref so `_close_socket` setting attributes to
        # None mid-loop doesn't crash us; we'll see EOF on the next
        # readline either way.
        rfile = self._rfile
        if rfile is None:
            return
        try:
            while not self._closed:
                try:
                    line = rfile.readline()
                except OSError, ValueError:
                    # ValueError: `close()` shut the file under a
                    # blocked readline ("I/O operation on closed file").
                    # Same outcome as a dropped socket (#699).
                    break
                if not line:
                    break  # Clean EOF from broker.
                try:
                    reply = Reply.model_validate_json(line)
                except Exception:
                    logger.error("broker reply parse failure; closing connection")
                    break
                with self._pending_lock:
                    fut = self._pending.pop(reply.rpc_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(reply)
                # rpc_id miss (e.g. server-side rpc_id=0 for a
                # MalformedJSON / InvalidRequest, or a late reply
                # for a caller that already timed out) is silently
                # dropped; the client never allocates 0 itself.
        finally:
            self._fail_all_pending("broker closed")
            with self._state_lock:
                self._close_socket()

    def _fail_all_pending(self, message: str) -> None:
        """Reject every pending future with BrokerUnavailable. Used
        by demux on EOF and by `close()` on shutdown. Idempotent."""
        with self._pending_lock:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(BrokerUnavailable(message))
            self._pending.clear()

    def _allocate_rpc_id(self) -> int:
        """Atomically allocate the next monotonic rpc_id. Client
        allocates from 1; broker uses 0 as sentinel for malformed-
        request replies."""
        with self._id_lock:
            rpc_id = self._next_rpc_id
            self._next_rpc_id += 1
            return rpc_id

    def _rpc(
        self,
        request_model: _BrokerRequest,
        *,
        timeout: float | None = None,
    ) -> Reply:
        """Allocate an rpc_id, register a future, send the request,
        await the reply. Per-RPC `timeout` (seconds) caps how long
        the caller waits; the socket itself has no read timeout.

        On socket failure during send, the future is removed from
        pending and BrokerUnavailable is raised immediately. On
        demux failure (EOF / unparseable reply) every pending
        future receives BrokerUnavailable from `_demux_loop`'s
        fail-all step.

        No automatic retry: the previous `_rpc`'s retry-once-across-
        reconnect loop is gone. Callers that need retry handle it
        at their layer (cache.set logs and swallows BrokerUnavailable;
        long-op CLI wrappers raise ClickException)."""
        rpc_id = self._allocate_rpc_id()
        request_model.rpc_id = rpc_id
        request_bytes = request_model.model_dump_json().encode("utf-8") + b"\n"

        fut: Future = Future()
        with self._pending_lock:
            self._pending[rpc_id] = fut

        try:
            self._ensure_alive()
            sock = self._sock
            if sock is None:
                raise BrokerUnavailable("socket closed during send")
            with self._send_lock:
                sock.sendall(request_bytes)
        except (OSError, BrokerUnavailable) as exc:
            with self._pending_lock:
                self._pending.pop(rpc_id, None)
            if not isinstance(exc, BrokerUnavailable):
                raise BrokerUnavailable(f"send failed: {exc}") from exc
            raise

        timeout_value = timeout if timeout is not None else request_model.timeout
        try:
            reply = fut.result(timeout=timeout_value)
        except FuturesTimeoutError as exc:
            # Caller's per-RPC timeout. Leave the pending entry in
            # place; if the reply eventually arrives, demux drops it.
            # If the socket dies, fail-all clears it.
            raise BrokerUnavailable(f"rpc timed out after {timeout_value}s") from exc
        return reply

    def call(self, req: _BrokerRequest, timeout: float | None = None) -> dict:
        """Send one request and return its reply's `result` payload
        (`{}` when the op returns none). The request model is the op's
        whole definition: its fields, and its `timeout` unless the
        caller overrides it. A failed reply raises `BrokerUnavailable`
        as `"<op>: <reply.error>"`; the admin CLI parses that format."""
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"{req.op}: {reply.error}")
        return reply.result or {}

    def backfill(self, req, result_cls, timeout: float | None = None):
        """Send one backfill chunk RPC. Returns `(result, run)`: the
        chunk's typed `BackfillResult`, and the run info dict described
        on `protocol.Reply.result` (start time, the cursor the chunk
        started from, examined across the whole run)."""
        result = self.call(req, timeout)
        return result_cls.model_validate(result.get("counters", {})), result.get("run")

    def close(self) -> None:
        """Tests and CLI tools call this to release the socket; the
        process-singleton accessor below normally never closes."""
        with self._state_lock:
            self._closed = True
            self._close_socket()
        self._fail_all_pending("client closed")


# Process-singleton, lazy-constructed on first use. Lookup is
# guarded by a lock so the first call from two threads at once
# (unlikely with sync workers but cheap insurance) doesn't open
# two sockets.
_client_lock = threading.Lock()
_client: BrokerClient | None = None


def get_broker_client() -> BrokerClient:
    """Return the process-singleton client, constructing it on
    first call. Reads `settings.broker_socket_path` at first call
    time. The setting has a default of `/data/.broker.sock` post-
    2.0.0; the only failure mode here is "socket path is empty
    (operator override)" which `BrokerClient`'s connect retry will
    surface as a `BrokerUnavailable` on the first RPC."""
    global _client
    with _client_lock:
        if _client is None:
            _client = BrokerClient(settings.broker_socket_path)
        return _client


def reset_broker_client() -> None:
    """Tear down the singleton. Test-only entry point; production
    code never calls this. Returns silently if no client was
    constructed."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None
