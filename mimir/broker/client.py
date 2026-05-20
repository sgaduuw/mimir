"""Broker client: talk to the broker daemon from another process.

Process-singleton (`get_broker_client()`): each gunicorn worker /
each CLI invocation reuses one persistent socket connection.

Thread-safe via a per-client `threading.Lock` wrapping every RPC.
Gunicorn's sync workers are single-threaded, but the scheduler-
sidecar's `warm-cache` (and other CLI commands invoking the
ThreadPoolExecutor) fans out across N worker threads sharing the
same process-singleton client. Without the lock, concurrent RPCs
would race on the same socket: interleaved writes break JSONL
framing, the broker returns parse errors, the client closes the
socket, and every thread piles into a fresh connect attempt
against the broker's listen backlog (production saw `Errno 11
Resource temporarily unavailable` on connect under load). The
lock serializes RPCs in-process; the broker itself is already
single-threaded so the bottleneck is unchanged, but framing and
connect pressure stay clean.

Reconnect: on any socket error mid-RPC the client closes the
socket, marks itself disconnected, and retries the same RPC once
on a fresh connection. Two consecutive failures raise
`BrokerUnavailable`. Backoff is hardcoded short (~100ms) because
the broker is on the same host; long backoffs would just stretch
out web-tier request latency under broker-restart windows.

Each RPC has a 5s timeout (`SO_RCVTIMEO`). The broker handles
requests serially on one thread; 5s comfortably covers the
worst-case (a slow `cache_delete_for_inbox` on a large cache
table) without blocking callers indefinitely if the broker hangs.
"""
import logging
import socket
import threading
from pathlib import Path

from mimir.broker.protocol import (
    BootstrapInboxesRequest,
    CacheDeleteForInboxRequest,
    CacheDeleteRequest,
    CachePurgeExpiredRequest,
    CacheSetRequest,
    IngestInboxRequest,
    PingRequest,
    Reply,
)
from mimir.config import settings

logger = logging.getLogger(__name__)


# Default per-RPC timeout. Broker is on the same host on a UNIX
# socket; 5s is generous against any single cache op the broker
# handles. Long ops override this per-call via the `timeout=`
# argument on the matching client method (e.g.
# `bootstrap_inboxes(timeout=300)` for a 5-minute ceiling).
RPC_TIMEOUT_SEC = 5.0


class BrokerUnavailable(Exception):
    """Raised when the broker socket can't be reached or two
    consecutive RPCs failed. Callers in `mimir.cache` catch this and
    log+drop, matching today's best-effort `OperationalError`
    semantics."""


class BrokerClient:
    """Persistent connection to the broker daemon. One per
    process; obtain via `get_broker_client()`."""

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = Path(socket_path)
        self._sock: socket.socket | None = None
        self._rfile = None
        self._wfile = None
        # Serialises every RPC through the singleton from multiple
        # caller threads (warm-cache's ThreadPoolExecutor fans out
        # to ~8 workers sharing this client). Held for the duration
        # of `_rpc`, which covers connect, write, read, and any
        # retry. Cheap on the happy path (uncontended); essential
        # under contention.
        self._rpc_lock = threading.Lock()

    def _connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(RPC_TIMEOUT_SEC)
        try:
            s.connect(str(self._socket_path))
        except OSError as exc:
            s.close()
            raise BrokerUnavailable(
                f"connect {self._socket_path}: {exc}"
            ) from exc
        self._sock = s
        # Buffered file wrappers for line-oriented JSONL framing.
        # `newline=""` because we frame on `\n` ourselves and don't
        # want Python's universal-newline translation rewriting it.
        self._rfile = s.makefile("rb", buffering=0)
        self._wfile = s.makefile("wb", buffering=0)

    def _close(self) -> None:
        for f in (self._rfile, self._wfile):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._rfile = None
        self._wfile = None

    def _send_one(self, request_json: str) -> Reply:
        """One attempt: write the request, read one reply line.
        Raises any socket / framing error to the caller; the public
        wrappers handle retry.

        Uses `socket.sendall` (loops on partial sends) rather than
        the `_wfile.write` path. The earlier shape called
        `makefile("wb", buffering=0).write(...)`, which delegates to
        `SocketIO.write`, which does a single `send()` and **returns
        the number of bytes actually written**. For small payloads
        that's the full request; for payloads larger than the
        kernel socket-send buffer (~208 KB on Linux by default,
        tripped easily by sitemap XML for inboxes with many
        articles), `send()` returns a short count and the leftover
        bytes are silently dropped. The broker then receives a
        truncated message and the JSON parser reports
        `Unterminated string`. Reported in production after the
        1.33.0 deploy on inbox sitemap writes.
        """
        assert self._sock is not None and self._rfile is not None
        self._sock.sendall(request_json.encode("utf-8") + b"\n")
        line = self._rfile.readline()
        if not line:
            raise BrokerUnavailable("broker closed the connection")
        return Reply.model_validate_json(line)

    def _rpc(
        self,
        request_json: str,
        *,
        timeout: float | None = None,
    ) -> Reply:
        """Send the request, read the reply. Retries once across a
        reconnect on socket errors; raises `BrokerUnavailable` on
        the second failure.

        Held inside `_rpc_lock` so concurrent calls from multiple
        threads (warm-cache's ThreadPoolExecutor) serialize cleanly
        on this client's socket. The broker is single-threaded
        upstream anyway, so the lock doesn't reduce throughput,
        just keeps framing intact and avoids connect storms when
        each thread races to reopen the socket after a framing
        error.

        `timeout`: when not None, set the socket's per-RPC timeout
        to this value (seconds) for the duration of the call, then
        restore the default `RPC_TIMEOUT_SEC` afterwards. Long ops
        (Phase 2.0+: `bootstrap_inboxes`, ingest, backfills, ...)
        pass a much larger value (minutes) since their reply only
        arrives once the work completes. Cache ops use the default.
        """
        with self._rpc_lock:
            last_exc: Exception | None = None
            for attempt in range(2):
                if self._sock is None:
                    try:
                        self._connect()
                    except BrokerUnavailable as exc:
                        last_exc = exc
                        continue
                if timeout is not None and self._sock is not None:
                    # Override the default 5s for this RPC. Restored
                    # in the `finally` below so subsequent RPCs on
                    # the same socket go back to the default.
                    self._sock.settimeout(timeout)
                try:
                    try:
                        return self._send_one(request_json)
                    except (OSError, BrokerUnavailable) as exc:
                        last_exc = exc
                        self._close()
                        # Fall through to retry on a fresh connection.
                        continue
                finally:
                    if timeout is not None and self._sock is not None:
                        self._sock.settimeout(RPC_TIMEOUT_SEC)
            raise BrokerUnavailable(
                f"broker rpc failed after retry: {last_exc}"
            )

    # Public ops ─────────────────────────────────────────────

    def cache_set(self, key: str, value_json: str, ttl: int) -> None:
        req = CacheSetRequest(key=key, value_json=value_json, ttl=ttl)
        reply = self._rpc(req.model_dump_json())
        if not reply.ok:
            raise BrokerUnavailable(f"cache_set: {reply.error}")

    def cache_delete(self, key: str) -> int:
        req = CacheDeleteRequest(key=key)
        reply = self._rpc(req.model_dump_json())
        if not reply.ok:
            raise BrokerUnavailable(f"cache_delete: {reply.error}")
        return reply.rows_deleted or 0

    def cache_delete_for_inbox(self, name: str) -> int:
        req = CacheDeleteForInboxRequest(name=name)
        reply = self._rpc(req.model_dump_json())
        if not reply.ok:
            raise BrokerUnavailable(f"cache_delete_for_inbox: {reply.error}")
        return reply.rows_deleted or 0

    def cache_purge_expired(self) -> int:
        req = CachePurgeExpiredRequest()
        reply = self._rpc(req.model_dump_json())
        if not reply.ok:
            raise BrokerUnavailable(f"cache_purge_expired: {reply.error}")
        return reply.rows_deleted or 0

    def ping(self) -> bool:
        req = PingRequest()
        reply = self._rpc(req.model_dump_json())
        return bool(reply.ok)

    # Long ops ─────────────────────────────────────────────────────────

    def ingest_inbox(
        self,
        inbox_name: str,
        *,
        limit: int | None = None,
        workers: int | None = None,
        timeout: float = 3600.0,
    ) -> list:
        """Phase 2.1 long op: run `ingest_inbox(name)` on the broker.
        Returns the per-epoch `IngestResult` list (reconstructed
        from the JSON payload so the CLI sees the same shape as
        the direct path).

        Default `timeout=3600s` (1 hour) covers a fresh-deploy
        per-inbox ingest of an unindexed mirror; smaller per-tick
        ingests complete in seconds. Operators on huge deploys can
        bump it via the per-call kwarg.
        """
        from mimir.ingest.epoch import IngestResult
        req = IngestInboxRequest(
            inbox_name=inbox_name, limit=limit, workers=workers,
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"ingest_inbox: {reply.error}")
        raw = (reply.result or {}).get("results", [])
        return [IngestResult.model_validate(r) for r in raw]

    def bootstrap_inboxes(self, *, timeout: float = 60.0) -> int:
        """Tell the broker to reconcile env-configured inboxes into
        the DB. Returns the number of inboxes the env declares (so
        the CLI can echo the same line whether running broker-mode
        or direct). Phase 2.0 long op; smallest op in the long
        family and the migration canary for the per-op-timeout
        machinery. Default `timeout=60s` is generous (the operation
        is one upsert per inbox)."""
        req = BootstrapInboxesRequest()
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"bootstrap_inboxes: {reply.error}")
        return int((reply.result or {}).get("inboxes", 0))

    def close(self) -> None:
        """Tests and CLI tools call this to release the socket; the
        process-singleton accessor below normally never closes."""
        self._close()


# Process-singleton, lazy-constructed on first use. Lookup is
# guarded by a lock so the first call from two threads at once
# (unlikely with sync workers but cheap insurance) doesn't open
# two sockets.
_client_lock = threading.Lock()
_client: BrokerClient | None = None


def get_broker_client() -> BrokerClient:
    """Return the process-singleton client, constructing it on
    first call. Reads `settings.broker_socket_path` at first call
    time; if that's unset, raises (callers should check the
    setting before reaching this)."""
    global _client
    with _client_lock:
        if _client is None:
            if settings.broker_socket_path is None:
                raise BrokerUnavailable("broker_socket_path unset")
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
