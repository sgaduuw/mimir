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
from concurrent.futures import Future, TimeoutError as FuturesTimeoutError
from pathlib import Path

from mimir.broker.protocol import (
    AnalyzeRequest,
    BackfillArticleFilesRequest,
    BackfillArticleTrailersRequest,
    BackfillCanonicalsRequest,
    BackfillPatchSeriesRequest,
    BootstrapInboxesRequest,
    CacheDeleteForInboxRequest,
    CacheDeleteRequest,
    CachePurgeExpiredRequest,
    CacheSetRequest,
    FailuresReplayRequest,
    InboxAddTrackedAuthorRequest,
    InboxClearTrackedAuthorsRequest,
    InboxCreateRequest,
    InboxDeleteRequest,
    InboxRemoveTrackedAuthorRequest,
    InboxSetTrackedAuthorsRequest,
    InboxUpdateRequest,
    IngestInboxRequest,
    PingRequest,
    Reply,
    RobotsAddRequest,
    RobotsRemoveRequest,
    RobotsResetRequest,
    RobotsUpdateRequest,
    UpdateMainlineRequest,
    VacuumRequest,
    WarmGlobalRequest,
    WarmInboxRequest,
    WarmSubsystemRequest,
    _BrokerRequest,
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
            raise BrokerUnavailable(f"connect {self._socket_path}: {exc}") from exc
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
                t = threading.Thread(
                    target=self._demux_loop,
                    daemon=True,
                    name=f"broker-client-demux-{self._socket_path.name}",
                )
                self._demux_thread = t
                t.start()

    def _demux_loop(self) -> None:
        """Read JSONL replies in a loop; resolve the matching
        pending future. On EOF or OSError, fails every pending
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
                except OSError:
                    break
                if not line:
                    break  # Clean EOF from broker.
                try:
                    reply = Reply.model_validate_json(line)
                except Exception:
                    logger.error(
                        "broker reply parse failure; closing connection"
                    )
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

        timeout_value = timeout if timeout is not None else RPC_TIMEOUT_SEC
        try:
            reply = fut.result(timeout=timeout_value)
        except FuturesTimeoutError as exc:
            # Caller's per-RPC timeout. Leave the pending entry in
            # place; if the reply eventually arrives, demux drops it.
            # If the socket dies, fail-all clears it.
            raise BrokerUnavailable(
                f"rpc timed out after {timeout_value}s"
            ) from exc
        return reply

    # Public ops ─────────────────────────────────────────────

    def cache_set(self, key: str, value_json: str, ttl: int) -> None:
        req = CacheSetRequest(rpc_id=0, key=key, value_json=value_json, ttl=ttl)
        reply = self._rpc(req)
        if not reply.ok:
            raise BrokerUnavailable(f"cache_set: {reply.error}")

    def cache_delete(self, key: str) -> int:
        req = CacheDeleteRequest(rpc_id=0, key=key)
        reply = self._rpc(req)
        if not reply.ok:
            raise BrokerUnavailable(f"cache_delete: {reply.error}")
        return reply.rows_deleted or 0

    def cache_delete_for_inbox(self, name: str) -> int:
        req = CacheDeleteForInboxRequest(rpc_id=0, name=name)
        reply = self._rpc(req)
        if not reply.ok:
            raise BrokerUnavailable(f"cache_delete_for_inbox: {reply.error}")
        return reply.rows_deleted or 0

    def cache_purge_expired(self) -> int:
        req = CachePurgeExpiredRequest(rpc_id=0)
        reply = self._rpc(req)
        if not reply.ok:
            raise BrokerUnavailable(f"cache_purge_expired: {reply.error}")
        return reply.rows_deleted or 0

    def ping(self) -> bool:
        req = PingRequest(rpc_id=0)
        reply = self._rpc(req)
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
            rpc_id=0,
            inbox_name=inbox_name,
            limit=limit,
            workers=workers,
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"ingest_inbox: {reply.error}")
        raw = (reply.result or {}).get("results", [])
        return [IngestResult.model_validate(r) for r in raw]

    def backfill_article_files(
        self,
        *,
        limit: int | None = None,
        reprocess: bool = False,
        continuation: int | None = None,
        timeout: float = 3600.0,
    ):
        """Phase 2.2 chunked backfill RPC. One call advances by at
        most one broker chunk (`Settings.broker_backfill_chunk_seconds`
        seconds of walker time, default 10 s). Returns a
        `mimir.patches.BackfillResult` with `partial` + `continuation`
        set; the CLI loops on `partial=True` aggregating per-chunk
        counters via `BackfillResult.merge`.

        Default `timeout=3600 s` ceiling per RPC. The handler aims
        to return well under the chunk seconds setting, so 1 hour
        is a generous safety net catching a pathologically slow
        per-row case (e.g. mirror IO during a blob fetch); a
        properly-tuned chunk should never approach it."""
        from mimir.patches import BackfillResult

        req = BackfillArticleFilesRequest(
            rpc_id=0,
            limit=limit,
            reprocess=reprocess,
            continuation=continuation,
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"backfill_article_files: {reply.error}")
        counters = (reply.result or {}).get("counters", {})
        return BackfillResult.model_validate(counters)

    def backfill_article_trailers(
        self,
        *,
        limit: int | None = None,
        reprocess: bool = False,
        continuation: int | None = None,
        timeout: float = 3600.0,
    ):
        """Phase 2.2 chunked backfill RPC; see
        `backfill_article_files` for the chunk/resume contract."""
        from mimir.trailers import BackfillResult

        req = BackfillArticleTrailersRequest(
            rpc_id=0,
            limit=limit,
            reprocess=reprocess,
            continuation=continuation,
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"backfill_article_trailers: {reply.error}")
        counters = (reply.result or {}).get("counters", {})
        return BackfillResult.model_validate(counters)

    def backfill_patch_series(
        self,
        *,
        limit: int | None = None,
        reprocess: bool = False,
        continuation: int | None = None,
        timeout: float = 3600.0,
    ):
        """Phase 2.2 chunked backfill RPC; see
        `backfill_article_files` for the chunk/resume contract."""
        from mimir.patch_series import BackfillResult

        req = BackfillPatchSeriesRequest(
            rpc_id=0,
            limit=limit,
            reprocess=reprocess,
            continuation=continuation,
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"backfill_patch_series: {reply.error}")
        counters = (reply.result or {}).get("counters", {})
        return BackfillResult.model_validate(counters)

    def backfill_canonicals(
        self,
        *,
        inbox_filter: str | None = None,
        limit: int | None = None,
        reprocess: bool = False,
        continuation: int | None = None,
        timeout: float = 3600.0,
    ):
        """Phase 2.2 chunked backfill RPC for
        `articles.canonical_inbox_id`; see `backfill_article_files`
        for the chunk/resume contract."""
        from mimir.ingest.backfill import BackfillResult

        req = BackfillCanonicalsRequest(
            rpc_id=0,
            inbox_filter=inbox_filter,
            limit=limit,
            reprocess=reprocess,
            continuation=continuation,
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"backfill_canonicals: {reply.error}")
        counters = (reply.result or {}).get("counters", {})
        return BackfillResult.model_validate(counters)

    def warm_inbox(
        self,
        inbox_name: str,
        *,
        targets: list[str] | None = None,
        priority: int = 1,
        timeout: float = 300.0,
    ) -> dict:
        """Phase 2.2 warm op: warm one inbox's cached helpers via
        the broker. Returns the reply's result dict
        (`{warmed, elapsed_ms, errors}`); the per-target `errors`
        list captures helpers that raised, mirroring the best-
        effort posture of `_warm_after_ingest`.

        `targets` (optional) narrows the helper set to a labelled
        subset, matching the post-ingest warm scope. None warms
        every per-inbox helper, which is the warm-cache CLI
        posture.

        `priority` controls broker warm-queue ordering (Task 5 of
        the fast/slow tier split, spec §2): 0 = fast (sitemap-class;
        jumps ahead of queued slow items via `queue.PriorityQueue`),
        1 = slow (default; matches today's single-tier FIFO
        behaviour for any caller that doesn't set it explicitly).

        Default timeout 300 s: typical per-inbox warm finishes in
        seconds on the production corpus; 5 minutes is a generous
        safety net for outliers (e.g. a brand-new inbox's first
        warm where every helper is cold). Routed to the broker's
        N warm-workers so multiple inboxes are warmed concurrently.
        """
        req = WarmInboxRequest(
            rpc_id=0, inbox_name=inbox_name, targets=targets, priority=priority
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"warm_inbox: {reply.error}")
        return reply.result or {}

    def warm_subsystem(
        self,
        inbox_name: str,
        subsystem_id: int,
        *,
        priority: int = 1,
        timeout: float = 300.0,
    ) -> dict:
        """Per-(inbox, subsystem) warm RPC. Used by the slow-tier
        dispatch path in `mimir warm-cache --tier slow` to fan out
        subsystem-dashboards work across broker workers rather than
        serialising it inside a single warm_inbox RPC.

        Reply shape mirrors `warm_inbox`: `{warmed, elapsed_ms,
        errors}`. The handler runs the four dashboard helpers + the
        two triage queues + per-reviewer `articles_reviewed_by`
        warmups for the one subsystem.

        `priority` controls broker warm-queue ordering identical to
        `warm_inbox.priority`: 0 = fast, 1 = slow (default; only
        existing caller is the slow-tier fan-out)."""
        req = WarmSubsystemRequest(
            rpc_id=0,
            inbox_name=inbox_name,
            subsystem_id=subsystem_id,
            priority=priority,
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"warm_subsystem: {reply.error}")
        return reply.result or {}

    def warm_global(
        self,
        *,
        targets: list[str] | None = None,
        priority: int = 1,
        timeout: float = 300.0,
    ) -> dict:
        """Phase 2.2 warm op: warm the cross-inbox aggregators
        (`most_active_subsystems_global` + sitemap index/meta when
        SITE_BASE_URL is set). Caller MUST issue this after every
        warm_inbox in the same cycle has completed, otherwise the
        aggregator races a warm-worker still mid-compute. The CLI
        dispatcher in `mimir.cli.cache.warm_cache_command` handles
        this sequencing.

        `targets` (Task 5 of the fast/slow tier split) narrows the
        global aggregator set to a labelled subset, mirroring
        `warm_inbox`. None = run every global aggregator (today's
        shape). The CLI dispatches a narrowed list under
        `--tier fast` / `--tier slow` so each scheduler cadence
        only refreshes its own global surfaces.

        `priority` mirrors `warm_inbox.priority`: 0 = fast, 1 = slow
        (default).
        """
        req = WarmGlobalRequest(rpc_id=0, targets=targets, priority=priority)
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"warm_global: {reply.error}")
        return reply.result or {}

    def update_mainline(
        self,
        *,
        skip_fetch: bool = False,
        skip_maintainers: bool = False,
        skip_commits: bool = False,
        force: bool = False,
        timeout: float = 600.0,
    ) -> dict:
        """Phase 2.3 long op: refresh the mainline tree + reload
        MAINTAINERS + walk Link-trailers via the broker. Returns the
        `UpdateMainlineResult` dict so the CLI can echo the same
        outcome lines as the direct path.

        Default `timeout=600 s` (10 min): the steady-state tick
        short-circuits the MAINTAINERS reparse and walks ~zero new
        commits, finishing in seconds; the slow path is the first
        run on a fresh deploy, which walks ~1.5M Linus-tree commits
        and can run several minutes."""
        req = UpdateMainlineRequest(
            rpc_id=0,
            skip_fetch=skip_fetch,
            skip_maintainers=skip_maintainers,
            skip_commits=skip_commits,
            force=force,
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"update_mainline: {reply.error}")
        return reply.result or {}

    def analyze(self, *, full: bool = False, timeout: float = 600.0) -> dict:
        """Phase 2.3 long op: run ANALYZE on the broker. Returns the
        `AnalyzeResult` dict (`{full, elapsed_ms}`).

        Default `timeout=600 s` covers both the bounded daily pass
        (~1-3 s) and the weekly `full=True` pass (~25-30 s) with
        plenty of headroom for growth."""
        req = AnalyzeRequest(rpc_id=0, full=full)
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"analyze: {reply.error}")
        return reply.result or {}

    def vacuum(self, *, timeout: float = 3600.0) -> dict:
        """Phase 2.3 long op: run VACUUM on the broker. Returns the
        `VacuumResult` dict (`{elapsed_ms, db_size_before,
        db_size_after, reclaimed}`).

        Default `timeout=3600 s` (1 hour): VACUUM scales with the
        on-disk DB size; a multi-GB corpus can take minutes, and
        the per-call timeout caps the worst case rather than letting
        a runaway lock the CLI indefinitely. Operators on huge
        deploys can bump via the per-call kwarg.

        While this RPC is in flight, every other broker worker is
        paused (SQLite exclusive lock); cache writes from the web
        tier may time out on the client side. The broker logs a
        WARNING at start so the cause is correlatable."""
        req = VacuumRequest(rpc_id=0)
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"vacuum: {reply.error}")
        return reply.result or {}

    def bootstrap_inboxes(self, *, timeout: float = 60.0) -> int:
        """Tell the broker to reconcile env-configured inboxes into
        the DB. Returns the number of inboxes the env declares (so
        the CLI can echo the same line whether running broker-mode
        or direct). Phase 2.0 long op; smallest op in the long
        family and the migration canary for the per-op-timeout
        machinery. Default `timeout=60s` is generous (the operation
        is one upsert per inbox)."""
        req = BootstrapInboxesRequest(rpc_id=0)
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"bootstrap_inboxes: {reply.error}")
        return int((reply.result or {}).get("inboxes", 0))

    # Admin-write ops (Phase 2.4) ─────────────────────────────────────

    def inbox_create(
        self,
        name: str,
        mirror_path: str,
        upstream_url: str,
        *,
        timeout: float = 60.0,
    ) -> dict:
        """Create one inbox via the broker. Returns the resulting
        inbox dict (`{id, name, mirror_path, upstream_url,
        tracked_authors}`) reconstructed from the Reply payload."""
        req = InboxCreateRequest(
            rpc_id=0,
            name=name,
            mirror_path=mirror_path,
            upstream_url=upstream_url,
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"inbox_create: {reply.error}")
        return (reply.result or {}).get("inbox", {})

    def inbox_update(
        self,
        name: str,
        *,
        new_name: str | None = None,
        mirror_path: str | None = None,
        upstream_url: str | None = None,
        timeout: float = 60.0,
    ) -> dict:
        """Modify one inbox via the broker. Only non-None fields are
        applied server-side."""
        req = InboxUpdateRequest(
            rpc_id=0,
            name=name,
            new_name=new_name,
            mirror_path=mirror_path,
            upstream_url=upstream_url,
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"inbox_update: {reply.error}")
        return (reply.result or {}).get("inbox", {})

    def inbox_delete(
        self,
        name: str,
        *,
        keep_orphan_articles: bool = False,
        remove_inbox_data: bool = False,
        timeout: float = 600.0,
    ) -> dict:
        """Remove one inbox via the broker. Returns the removal
        report dict (`{name, article_lists_deleted,
        ingest_state_deleted, orphan_articles_deleted,
        mirror_path_deleted}`). Operator confirmation lives in the
        CLI; this method only sends the post-confirmation request.

        Default `timeout=600 s`: with `remove_inbox_data=True` the
        broker `rm -rf`s the on-disk mirror, which can take a few
        minutes on a ~20 GB lkml-shaped tree."""
        req = InboxDeleteRequest(
            rpc_id=0,
            name=name,
            keep_orphan_articles=keep_orphan_articles,
            remove_inbox_data=remove_inbox_data,
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"inbox_delete: {reply.error}")
        return (reply.result or {}).get("report", {})

    def inbox_set_tracked_authors(
        self,
        name: str,
        trackers: dict[str, str],
        *,
        timeout: float = 60.0,
    ) -> dict:
        """Replace the per-inbox tracker dict in one shot."""
        req = InboxSetTrackedAuthorsRequest(rpc_id=0, name=name, trackers=trackers)
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"inbox_set_tracked_authors: {reply.error}")
        return (reply.result or {}).get("inbox", {})

    def inbox_add_tracked_author(
        self,
        name: str,
        label: str,
        substring: str,
        *,
        timeout: float = 60.0,
    ) -> dict:
        """Add (or replace) one tracker entry."""
        req = InboxAddTrackedAuthorRequest(
            rpc_id=0,
            name=name,
            label=label,
            substring=substring,
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"inbox_add_tracked_author: {reply.error}")
        return (reply.result or {}).get("inbox", {})

    def inbox_remove_tracked_author(
        self,
        name: str,
        label: str,
        *,
        timeout: float = 60.0,
    ) -> dict:
        """Remove one tracker entry by label."""
        req = InboxRemoveTrackedAuthorRequest(rpc_id=0, name=name, label=label)
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"inbox_remove_tracked_author: {reply.error}")
        return (reply.result or {}).get("inbox", {})

    def inbox_clear_tracked_authors(
        self,
        name: str,
        *,
        timeout: float = 60.0,
    ) -> dict:
        """Drop all tracker entries (writes NULL)."""
        req = InboxClearTrackedAuthorsRequest(rpc_id=0, name=name)
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"inbox_clear_tracked_authors: {reply.error}")
        return (reply.result or {}).get("inbox", {})

    def robots_add(
        self,
        user_agent: str,
        *,
        disallow: list[str] | None = None,
        crawl_delay: int | None = None,
        content_signals: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> dict:
        """Insert one robots_rules row via the broker. Returns the
        resulting rule dict (`{user_agent, crawl_delay,
        disallow_paths, content_signals}`)."""
        req = RobotsAddRequest(
            rpc_id=0,
            user_agent=user_agent,
            disallow=list(disallow or []),
            crawl_delay=crawl_delay,
            content_signals=dict(content_signals or {}),
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"robots_add: {reply.error}")
        return (reply.result or {}).get("rule", {})

    def robots_update(
        self,
        user_agent: str,
        *,
        add_disallow: list[str] | None = None,
        remove_disallow: list[str] | None = None,
        crawl_delay: int | None = None,
        clear_crawl_delay: bool = False,
        set_content_signal: dict[str, str] | None = None,
        clear_content_signal: list[str] | None = None,
        clear_all_content_signals: bool = False,
        timeout: float = 60.0,
    ) -> dict:
        """Mutate one robots_rules row via the broker. `clear_crawl_delay`
        is distinct from `crawl_delay=None`: the former writes NULL,
        the latter leaves the column untouched. Content-Signal
        mutations parallel: `set_content_signal` upserts keys;
        `clear_content_signal` drops specific keys;
        `clear_all_content_signals` wipes the dict."""
        req = RobotsUpdateRequest(
            rpc_id=0,
            user_agent=user_agent,
            add_disallow=list(add_disallow or []),
            remove_disallow=list(remove_disallow or []),
            crawl_delay=crawl_delay,
            clear_crawl_delay=clear_crawl_delay,
            set_content_signal=dict(set_content_signal or {}),
            clear_content_signal=list(clear_content_signal or []),
            clear_all_content_signals=clear_all_content_signals,
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"robots_update: {reply.error}")
        return (reply.result or {}).get("rule", {})

    def robots_remove(
        self,
        user_agent: str,
        *,
        timeout: float = 60.0,
    ) -> None:
        """Drop one robots_rules row via the broker. `*` is refused
        server-side; use `robots_reset` to restore defaults."""
        req = RobotsRemoveRequest(rpc_id=0, user_agent=user_agent)
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"robots_remove: {reply.error}")

    def robots_reset(self, *, timeout: float = 60.0) -> None:
        """Drop every robots_rules row and re-seed the `*` stanza
        with the migration defaults."""
        req = RobotsResetRequest(rpc_id=0)
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"robots_reset: {reply.error}")

    def failures_replay(
        self,
        inbox_name: str,
        *,
        epoch_filter: str | None = None,
        limit: int | None = None,
        timeout: float = 1800.0,
    ) -> dict:
        """Replay persisted parse_failures for one inbox via the
        broker. Returns the ReplayResult dict
        (`{attempted, recovered, still_failed, skipped}`).

        Default `timeout=1800 s` (30 min) covers a multi-thousand-
        row replay session on a fresh post-fix run; steady-state
        replays are sub-second."""
        req = FailuresReplayRequest(
            rpc_id=0,
            inbox_name=inbox_name,
            epoch_filter=epoch_filter,
            limit=limit,
        )
        reply = self._rpc(req, timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"failures_replay: {reply.error}")
        return reply.result or {}

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
