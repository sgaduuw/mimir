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
            raise BrokerUnavailable(f"connect {self._socket_path}: {exc}") from exc
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
            raise BrokerUnavailable(f"broker rpc failed after retry: {last_exc}")

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
            inbox_name=inbox_name,
            limit=limit,
            workers=workers,
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
            limit=limit,
            reprocess=reprocess,
            continuation=continuation,
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
            limit=limit,
            reprocess=reprocess,
            continuation=continuation,
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
            limit=limit,
            reprocess=reprocess,
            continuation=continuation,
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
            inbox_filter=inbox_filter,
            limit=limit,
            reprocess=reprocess,
            continuation=continuation,
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
            inbox_name=inbox_name, targets=targets, priority=priority
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"warm_inbox: {reply.error}")
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
        req = WarmGlobalRequest(targets=targets, priority=priority)
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
            skip_fetch=skip_fetch,
            skip_maintainers=skip_maintainers,
            skip_commits=skip_commits,
            force=force,
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"update_mainline: {reply.error}")
        return reply.result or {}

    def analyze(self, *, full: bool = False, timeout: float = 600.0) -> dict:
        """Phase 2.3 long op: run ANALYZE on the broker. Returns the
        `AnalyzeResult` dict (`{full, elapsed_ms}`).

        Default `timeout=600 s` covers both the bounded daily pass
        (~1-3 s) and the weekly `full=True` pass (~25-30 s) with
        plenty of headroom for growth."""
        req = AnalyzeRequest(full=full)
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
        req = VacuumRequest()
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
        req = BootstrapInboxesRequest()
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
            name=name,
            mirror_path=mirror_path,
            upstream_url=upstream_url,
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
            name=name,
            new_name=new_name,
            mirror_path=mirror_path,
            upstream_url=upstream_url,
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
            name=name,
            keep_orphan_articles=keep_orphan_articles,
            remove_inbox_data=remove_inbox_data,
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
        req = InboxSetTrackedAuthorsRequest(name=name, trackers=trackers)
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
            name=name,
            label=label,
            substring=substring,
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
        req = InboxRemoveTrackedAuthorRequest(name=name, label=label)
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
        req = InboxClearTrackedAuthorsRequest(name=name)
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
            user_agent=user_agent,
            disallow=list(disallow or []),
            crawl_delay=crawl_delay,
            content_signals=dict(content_signals or {}),
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
            user_agent=user_agent,
            add_disallow=list(add_disallow or []),
            remove_disallow=list(remove_disallow or []),
            crawl_delay=crawl_delay,
            clear_crawl_delay=clear_crawl_delay,
            set_content_signal=dict(set_content_signal or {}),
            clear_content_signal=list(clear_content_signal or []),
            clear_all_content_signals=clear_all_content_signals,
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
        req = RobotsRemoveRequest(user_agent=user_agent)
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"robots_remove: {reply.error}")

    def robots_reset(self, *, timeout: float = 60.0) -> None:
        """Drop every robots_rules row and re-seed the `*` stanza
        with the migration defaults."""
        req = RobotsResetRequest()
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
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
            inbox_name=inbox_name,
            epoch_filter=epoch_filter,
            limit=limit,
        )
        reply = self._rpc(req.model_dump_json(), timeout=timeout)
        if not reply.ok:
            raise BrokerUnavailable(f"failures_replay: {reply.error}")
        return reply.result or {}

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
