"""Wire message shapes for the write-broker RPC.

JSONL over UNIX socket: one request per line, one reply per line.
Each `Request` model and the `Reply` model encode via
`model_dump_json()` and decode via `model_validate_json()`. The
top-level discriminator is the `op` field, dispatched at the broker
in `handlers.dispatch`.

Cache value encoding stays in `mimir.cache` (the `register()`-driven
type registry). The `value_json` field on `CacheSetRequest` carries
the already-encoded JSON string; the broker stores it verbatim and
the encoding side imports never reach the broker process.

Op kinds: **cache** ops (sub-ms commits; `cache_set`, `cache_delete`,
`cache_delete_for_inbox`, `cache_purge_expired`, `ping`) route to
the broker's cache worker. **Long** ops (commit batches that run
seconds to minutes; `bootstrap_inboxes`, `ingest_epoch`,
`backfill_*`, `update_mainline`, `analyze`, `vacuum`) route to the
broker's long worker. The two workers compete
for the SQLite writer lock at the SQLite level, so cache writes only
wait for the long worker's current commit batch, not the whole long
op. See `handlers.LONG_OPS` for the routing set.
"""

from typing import Annotated, Any, ClassVar, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator

from mimir._inbox_name import INBOX_NAME_RE
from mimir.robots import CONTENT_SIGNAL_KEYS, CONTENT_SIGNAL_VALUES, PATH_RE, UA_RE


def _validate_inbox_name(v: str) -> str:
    """Shared slug-regex validator used by every request type that
    carries an `Inbox.name`-shaped field. Re-enforcing slug shape
    at the wire boundary keeps a buggy or hostile peer from
    smuggling LIKE metacharacters / path components / weird casing
    into ops that compose the value into SQL or filesystem paths."""
    if not INBOX_NAME_RE.fullmatch(v):
        raise ValueError(
            "name must be lowercase alphanumeric/hyphen, "
            "1 to 64 chars, not starting or ending with a hyphen"
        )
    return v


# The wire type for an `Inbox.name`-shaped field: length bounds and
# the slug check travel together, so a model cannot declare one
# without the other.
InboxName = Annotated[
    str, Field(min_length=1, max_length=64), AfterValidator(_validate_inbox_name)
]


class _BrokerRequest(BaseModel):
    """Base for every broker Request type. Carries the `rpc_id` field
    that the client allocates and the broker echoes back into the
    Reply so the client can demux concurrent in-flight RPCs on one
    socket. 3.0.0 wire-protocol change.

    The default of 0 is client-side only: callers build requests
    without an id and `BrokerClient.call` overwrites it with the
    allocated one. The wire still requires the key, and `dispatch()`
    rejects a request without it before this model sees it."""

    rpc_id: int = Field(default=0, ge=0)
    # How long `BrokerClient.call` waits for this op's reply, in
    # seconds. A ClassVar, so it never goes on the wire. The broker is
    # on the same host on a UNIX socket, so 5 s is generous for a
    # cache op; the long ops override it below.
    timeout: ClassVar[float] = 5.0


class CacheSetRequest(_BrokerRequest):
    op: Literal["cache_set"] = "cache_set"
    key: str = Field(min_length=1, max_length=512)
    value_json: str
    ttl: int = Field(ge=0)

    @field_validator("value_json")
    @classmethod
    def _value_json_nonempty(cls, v: str) -> str:
        # Empty `value_json` would round-trip as JSON `null` and
        # collapse later cache.get calls into `None`-on-hit which
        # is indistinguishable from miss. Reject at the boundary so
        # the broker doesn't store useless rows.
        if not v:
            raise ValueError("value_json must be a non-empty JSON string")
        return v


class CacheDeleteRequest(_BrokerRequest):
    op: Literal["cache_delete"] = "cache_delete"
    key: str = Field(min_length=1, max_length=512)


class CacheDeleteForInboxRequest(_BrokerRequest):
    op: Literal["cache_delete_for_inbox"] = "cache_delete_for_inbox"
    # `cache.delete_for_inbox_via_writer` translates this name into a
    # `LIKE %name%` clause. The CLI write paths slug-validate via
    # `mimir.inboxes.validate_name` before the call ever reaches
    # cache, so LIKE-pattern metacharacters can't appear in
    # practice. Re-enforcing it at the wire boundary keeps a
    # buggy or hostile peer from passing `_:foo` (or any other
    # pattern) and matching unrelated rows.
    name: InboxName


class CachePurgeExpiredRequest(_BrokerRequest):
    op: Literal["cache_purge_expired"] = "cache_purge_expired"


class PingRequest(_BrokerRequest):
    op: Literal["ping"] = "ping"


class IngestInboxRequest(_BrokerRequest):
    """Phase 2.1 long op: run `ingest_inbox(name)` on the broker
    process. Carries the inbox NAME (not an Inbox ORM object;
    that doesn't pickle and isn't useful across the RPC anyway);
    the broker handler looks the row up from the DB before
    delegating to `mimir.ingest.orchestrate.ingest_inbox`.

    `limit` and `workers` mirror the CLI options. `None` limit
    means "run to completion" (per-inbox-no-cap, the steady-
    state scheduler tick shape). `workers` defaults to
    `DEFAULT_WORKERS` server-side."""

    op: Literal["ingest_inbox"] = "ingest_inbox"
    # A fresh deploy's first ingest of an unindexed mirror; per-tick
    # ingests finish in seconds.
    timeout: ClassVar[float] = 3600.0
    inbox_name: InboxName
    limit: int | None = Field(default=None, ge=0)
    workers: int | None = Field(default=None, ge=1)


class ReindexRequest(_BrokerRequest):
    op: Literal["reindex"] = "reindex"
    timeout: ClassVar[float] = 3600.0
    inbox_name: InboxName
    epoch: str = Field(pattern=r"^[0-9]+\.git$")
    from_scratch: bool = False
    workers: int | None = Field(default=None, ge=1)


class BootstrapInboxesRequest(_BrokerRequest):
    """Long op: reconcile `Settings.inboxes` env config into the
    `inboxes` table. Idempotent via `ON CONFLICT (name) DO NOTHING`.
    Smallest of the long ops and the migration canary for Phase 2.0
    (proves the long-worker + per-op-timeout path end-to-end before
    Phase 2.1 migrates the meatier ingest ops)."""

    op: Literal["bootstrap_inboxes"] = "bootstrap_inboxes"
    # One upsert per inbox.
    timeout: ClassVar[float] = 60.0


# Phase 2.2 long ops: the four backfills. Each shares the same RPC
# shape (limit + reprocess + start) because each one's CLI loop
# wants the same thing: ask the broker for a chunk, get back
# per-chunk counters, repeat until done.
#
# A backfill RPC handler runs for at most `Settings.broker_backfill_
# chunk_seconds` (default 10 s) before returning `partial=True`.
# Between chunks the broker's long-op worker is free, queued cache
# writes and other long ops get to run, then the CLI fires the next
# chunk. Multi-hour backfills no longer monopolise the long worker.
#
# The broker owns the cursor (#680): each chunk resumes the unfinished
# run for its backfill and parameters (`mimir.backfill_runs`), so the
# request carries no position. `start` is set on a CLI session's first
# RPC only: "resume" starts a run when there is none, "restart"
# discards the unfinished run and starts a new one at the newest
# article, unless the run has examined something, in which case the
# chunk walks nothing and the reply names the run it refused to
# discard. "force_restart" discards it regardless (the CLI's `--yes`).
# Any `start` is refused the same way when the run is live, its
# `updated_at` newer than `backfill_runs.LIVE_RUN_SEC`: another session
# is driving it.
# Later RPCs leave it None and never start a run, so a session whose
# run another session finished ends there. `limit` caps this request,
# not the run.


class BackfillArticleFilesRequest(_BrokerRequest):
    op: Literal["backfill_article_files"] = "backfill_article_files"
    # Per chunk. The handler returns within
    # `broker_backfill_chunk_seconds`, so an hour only catches a
    # pathologically slow row (mirror IO during a blob fetch).
    timeout: ClassVar[float] = 3600.0
    limit: int | None = Field(default=None, ge=0)
    reprocess: bool = False
    start: Literal["resume", "restart", "force_restart"] | None = None


class BackfillArticleTrailersRequest(_BrokerRequest):
    op: Literal["backfill_article_trailers"] = "backfill_article_trailers"
    timeout: ClassVar[float] = 3600.0
    limit: int | None = Field(default=None, ge=0)
    reprocess: bool = False
    start: Literal["resume", "restart", "force_restart"] | None = None


class BackfillPatchSeriesRequest(_BrokerRequest):
    op: Literal["backfill_patch_series"] = "backfill_patch_series"
    timeout: ClassVar[float] = 3600.0
    limit: int | None = Field(default=None, ge=0)
    reprocess: bool = False
    start: Literal["resume", "restart", "force_restart"] | None = None


class BackfillCanonicalsRequest(_BrokerRequest):
    """Phase 2.2 long op: chunked `backfill_canonicals` over the
    broker. Same shape as the patch-metadata backfills plus
    `inbox_filter` for the `admin canonicals backfill --inbox`
    surface."""

    op: Literal["backfill_canonicals"] = "backfill_canonicals"
    timeout: ClassVar[float] = 3600.0
    inbox_filter: InboxName | None = None
    limit: int | None = Field(default=None, ge=0)
    reprocess: bool = False
    start: Literal["resume", "restart", "force_restart"] | None = None


# Phase 2.3 long ops: the three periodic maintenance writers
# (update-mainline + analyze + vacuum). Like ingest and the
# backfills, these route to the long worker and contend with cache
# writes only at SQLite-level granularity (cache writes wait for
# the maintenance op's current commit, not the whole op). VACUUM is
# the load-bearing exception: it holds an exclusive lock for the
# entire run, which freezes every other worker for the duration.
# Acceptable trade-off vs the alternative (a direct-writer process
# co-existing with the broker, which is what the 2.0.0 cleanup
# pulls out anyway). Operator-visible window is minutes once a
# week.


class UpdateMainlineRequest(_BrokerRequest):
    """Run `mimir.mainline.update_mainline` on the broker. The four
    booleans mirror the CLI options exactly so the broker handler
    can delegate without translating.

    The MAINTAINERS reparse + Link-trailer walk both touch
    subsystems / mainline_commits writes; running on the broker
    keeps those writes inside the single-writer process and the
    cross-process snapshot-upgrade window stays closed."""

    op: Literal["update_mainline"] = "update_mainline"
    # The steady-state tick walks ~zero new commits in seconds; the
    # first run on a fresh deploy walks ~1.5M Linus-tree commits and
    # can take several minutes.
    timeout: ClassVar[float] = 600.0
    skip_fetch: bool = False
    skip_maintainers: bool = False
    skip_commits: bool = False
    force: bool = False
    rewalk: bool = False


class BackfillThreadRootsRequest(_BrokerRequest):
    """Populate `article_lists.thread_root_id` for rows that predate
    the column (W8). Per-inbox because the root is per-inbox; `inbox`
    of None means every configured inbox.

    A write, so it runs on the broker like every other backfill. Unlike
    the per-article walkers it is a handful of bulk UPDATE passes
    rather than a blob-reading walk, so it needs no chunk/resume
    protocol: each pass is a single statement the writer commits
    whole."""

    op: Literal["backfill_thread_roots"] = "backfill_thread_roots"
    # A cold run walks every inbox; the bulk UPDATEs make wall time
    # a function of corpus size.
    timeout: ClassVar[float] = 3600.0
    inbox: InboxName | None = None


class AnalyzeRequest(_BrokerRequest):
    """Run `ANALYZE` on the broker. `full=True` overrides the
    per-connection `analysis_limit` for this pass (no cap) so the
    weekly safety-net catches index distributions the daily bounded
    pass undersamples."""

    op: Literal["analyze"] = "analyze"
    # Covers the bounded daily pass (~10.8 s, measured 2026-08-04)
    # and the weekly `full=True` pass (~25-30 s when measured at 11M
    # rows, not re-measured at the current 28.8M) with headroom.
    timeout: ClassVar[float] = 600.0
    full: bool = False


class FailuresReplayRequest(_BrokerRequest):
    """Phase 2.4 long op: re-parse persisted parse_failures for one
    inbox. Successful parses insert the article and delete the
    failure row; failed parses bump `attempts` + `last_attempt`.
    Idempotent; safe to repeat.

    Required `inbox_name` mirrors the CLI's required positional;
    failures replay is always inbox-scoped to keep the work
    bounded (per-inbox parse-failure tables stay small, but the
    iteration shape opens one dulwich repo per epoch). Optional
    `epoch_filter` further narrows; optional `limit` caps the
    rows attempted in this RPC."""

    op: Literal["failures_replay"] = "failures_replay"
    # A multi-thousand-row replay after a parser fix; steady-state
    # replays are sub-second.
    timeout: ClassVar[float] = 1800.0
    inbox_name: InboxName
    epoch_filter: str | None = Field(default=None, min_length=1, max_length=16)
    limit: int | None = Field(default=None, ge=0)


# Phase 2.4 admin-inbox CRUD ops. Each one is a split RPC matching
# the same shape as the rest of the broker family: one pydantic
# type per logical op, dispatched to a per-op handler. All four
# route to the long queue alongside `bootstrap_inboxes` (sub-second
# writes, but conservative routing keeps them out of cache-queue
# contention).


class InboxCreateRequest(_BrokerRequest):
    """Insert a new Inbox after validating all three fields. Handler
    delegates to `mimir.inboxes.create_inbox`."""

    op: Literal["inbox_create"] = "inbox_create"
    timeout: ClassVar[float] = 60.0
    name: InboxName
    mirror_path: str = Field(min_length=1, max_length=512)
    upstream_url: str = Field(min_length=1, max_length=512)


class InboxUpdateRequest(_BrokerRequest):
    """Modify an existing inbox. Only the supplied fields are
    touched server-side. Handler delegates to
    `mimir.inboxes.update_inbox`."""

    op: Literal["inbox_update"] = "inbox_update"
    timeout: ClassVar[float] = 60.0
    name: InboxName
    new_name: InboxName | None = None
    mirror_path: str | None = Field(default=None, min_length=1, max_length=512)
    upstream_url: str | None = Field(default=None, min_length=1, max_length=512)
    # "" clears to NULL, None leaves it alone.
    list_address: str | None = Field(default=None, max_length=254)


class InboxDeleteRequest(_BrokerRequest):
    """Remove an inbox and its dependent rows. Cascades through
    `article_lists` / `ingest_state` via FK ondelete=CASCADE. With
    `keep_orphan_articles=False` (default), also deletes any
    articles left without remaining links. With
    `remove_inbox_data=True`, the broker `rm -rf`s the on-disk
    public-inbox mirror at `mirror_path`. The CLI is responsible
    for the operator confirmation prompt; this request only carries
    the post-confirmation intent."""

    op: Literal["inbox_delete"] = "inbox_delete"
    # With `remove_inbox_data=True` the broker `rm -rf`s the mirror,
    # which can take minutes on a ~20 GB lkml-shaped tree.
    timeout: ClassVar[float] = 600.0
    name: InboxName
    keep_orphan_articles: bool = False
    remove_inbox_data: bool = False


class InboxSetTrackedAuthorsRequest(_BrokerRequest):
    """Replace the per-inbox tracker dict in one shot. Handler
    delegates to `mimir.inboxes.set_tracked_authors`."""

    op: Literal["inbox_set_tracked_authors"] = "inbox_set_tracked_authors"
    timeout: ClassVar[float] = 60.0
    name: InboxName
    trackers: dict[str, str]


def _validate_robots_user_agent(v: str) -> str:
    if not UA_RE.fullmatch(v):
        raise ValueError(
            "user_agent must be 1-64 visible-ASCII characters with no whitespace"
        )
    return v


def _validate_robots_paths(v: list[str]) -> list[str]:
    for p in v:
        if not PATH_RE.fullmatch(p):
            raise ValueError(
                f"disallow path {p!r} must start with '/' or '*', "
                "1-256 visible-ASCII characters, no whitespace"
            )
    return v


def _validate_robots_content_signals(v: dict[str, str]) -> dict[str, str]:
    for k, val in v.items():
        if k not in CONTENT_SIGNAL_KEYS:
            raise ValueError(
                f"content_signal key {k!r} must be one of {sorted(CONTENT_SIGNAL_KEYS)}"
            )
        if val not in CONTENT_SIGNAL_VALUES:
            raise ValueError(
                f"content_signal value {val!r} must be one of "
                f"{sorted(CONTENT_SIGNAL_VALUES)}"
            )
    return v


def _validate_robots_content_signal_keys(v: list[str]) -> list[str]:
    for k in v:
        if k not in CONTENT_SIGNAL_KEYS:
            raise ValueError(
                f"content_signal key {k!r} must be one of {sorted(CONTENT_SIGNAL_KEYS)}"
            )
    return v


# Wire types for the robots fields, bounds and check in one place as
# with `InboxName`.
RobotsUserAgent = Annotated[
    str,
    Field(min_length=1, max_length=64),
    AfterValidator(_validate_robots_user_agent),
]
RobotsPaths = Annotated[
    list[str], Field(max_length=64), AfterValidator(_validate_robots_paths)
]
RobotsContentSignals = Annotated[
    dict[str, str], AfterValidator(_validate_robots_content_signals)
]
RobotsContentSignalKeys = Annotated[
    list[str],
    Field(max_length=8),
    AfterValidator(_validate_robots_content_signal_keys),
]


class RobotsAddRequest(_BrokerRequest):
    """Insert one new robots_rules row. Handler delegates to
    `mimir.robots.add_rule`."""

    op: Literal["robots_add"] = "robots_add"
    timeout: ClassVar[float] = 60.0
    user_agent: RobotsUserAgent
    disallow: RobotsPaths = Field(default_factory=list)
    crawl_delay: int | None = Field(default=None, ge=0, le=86_400)
    content_signals: RobotsContentSignals = Field(default_factory=dict)


class RobotsUpdateRequest(_BrokerRequest):
    """Mutate one existing robots_rules row. Handler delegates to
    `mimir.robots.update_rule`. `clear_crawl_delay=True` sets the
    column to NULL (distinct from omitting `crawl_delay` which means
    "don't touch"). Content-Signal mutations parallel disallow:
    `set_content_signal` upserts; `clear_content_signal` (list of
    keys) removes; `clear_all_content_signals` wipes to NULL."""

    op: Literal["robots_update"] = "robots_update"
    timeout: ClassVar[float] = 60.0
    user_agent: RobotsUserAgent
    add_disallow: RobotsPaths = Field(default_factory=list)
    remove_disallow: RobotsPaths = Field(default_factory=list)
    crawl_delay: int | None = Field(default=None, ge=0, le=86_400)
    clear_crawl_delay: bool = False
    set_content_signal: RobotsContentSignals = Field(default_factory=dict)
    clear_content_signal: RobotsContentSignalKeys = Field(default_factory=list)
    clear_all_content_signals: bool = False


class RobotsRemoveRequest(_BrokerRequest):
    """Drop one robots_rules row. `*` is refused at the service
    layer. Handler delegates to `mimir.robots.remove_rule`."""

    op: Literal["robots_remove"] = "robots_remove"
    timeout: ClassVar[float] = 60.0
    user_agent: RobotsUserAgent


class RobotsResetRequest(_BrokerRequest):
    """Drop every row and re-seed the `*` stanza with the migration's
    defaults. Handler delegates to `mimir.robots.reset_rules`."""

    op: Literal["robots_reset"] = "robots_reset"
    timeout: ClassVar[float] = 60.0


class VacuumRequest(_BrokerRequest):
    """Run `VACUUM` (+ WAL checkpoint) on the broker. Holds the
    SQLite exclusive lock for the duration; cache writes from the
    web tier queue behind it and may time out under the broker
    client's per-RPC ceiling on huge databases. Documented in
    CONTEXT.md as an accepted weekly-quiet-window trade-off."""

    op: Literal["vacuum"] = "vacuum"
    # Scales with on-disk DB size; a multi-GB corpus takes minutes.
    # The ceiling stops a runaway from holding the CLI forever.
    timeout: ClassVar[float] = 3600.0


# Phase 2.2 warm-queue ops: per-inbox + global warming. Routed to
# the broker's NEW third queue (`warm_queue`) with N multi-workers
# (default 4, env `BROKER_WARM_WORKERS`). Sibling to the cache and
# long queues but parallelised across workers: warm computes are
# read-heavy and benefit from concurrent execution, even though
# each one's final `cache.set` commit still funnels through the
# SQLite writer lock.
#
# Reply.result for both warm ops carries:
#   `{"warmed": [<label>, ...], "elapsed_ms": int,
#     "errors": [<"label: repr">, ...]}`
# A per-target exception is captured into `errors` rather than
# failing the whole RPC, mirroring `_warm_after_ingest`'s best-
# effort posture: one broken helper shouldn't take down the
# scheduler's warm cycle.


class WarmInboxRequest(_BrokerRequest):
    """Per-inbox warm: invoke every cached helper for one inbox.
    The handler calls `mimir.cli.cache._build_inbox_targets(inbox)`
    and runs each target on its own session. Used by:

    - The scheduler's `mimir warm-cache` CLI in broker mode (fans
      out N warm_inbox jobs in parallel across configured inboxes,
      drained by the broker's N warm-workers).
    - Post-ingest warm-after-ingest paths
      (`mimir.ingest.orchestrate`) so a freshly-ingested inbox's
      cache is hot before the next reader lands.

    `targets`, when non-None, narrows the per-inbox target set to
    a labelled subset. None = warm all helpers (the warm-cache CLI
    posture). Post-ingest warm uses a small subset
    (`active_threads`, `archive_stats`, `daily_volume`).

    `priority` controls broker warm-queue ordering (Task 5 of the
    fast/slow tier split, spec §2): 0 = fast tier (sitemap-class
    keys + cheap front-page helpers; jumps ahead of queued slow
    items via `queue.PriorityQueue`), 1 = slow tier (the default;
    matches today's single-tier FIFO behaviour for any caller that
    doesn't set it explicitly).
    """

    op: Literal["warm_inbox"] = "warm_inbox"
    # Seconds on the production corpus; five minutes covers a new
    # inbox's first warm, where every helper is cold.
    timeout: ClassVar[float] = 300.0
    inbox_name: InboxName
    targets: list[str] | None = None
    priority: int = Field(default=1, ge=0, le=1)


class WarmSubsystemRequest(_BrokerRequest):
    """Per-subsystem warm: invoke the four dashboard helpers
    (`recent_articles_in_subsystem`, `active_threads_in_subsystem`,
    `daily_volume_in_subsystem`, `active_reviewers_in_subsystem`)
    plus reviewer warmups (`articles_reviewed_by` per reviewer
    surfaced) for ONE (inbox, subsystem) pair. Used by:

    - The scheduler's `mimir warm-cache --tier slow` CLI, which
      pre-computes the top-N most-active subsystems per inbox and
      fans out one warm_subsystem RPC per (inbox, subsystem). Broker
      workers chew through them concurrently, parallelising what
      was previously serial inside one warm_inbox worker thread.

    `priority` controls broker warm-queue ordering identical to
    the `WarmInboxRequest.priority` field: 0 = fast, 1 = slow.
    Slow is the default; the only existing caller (slow-tier
    warm-cache) sets priority=1 explicitly.
    """

    op: Literal["warm_subsystem"] = "warm_subsystem"
    timeout: ClassVar[float] = 300.0
    inbox_name: InboxName
    subsystem_id: int = Field(ge=1)
    priority: int = Field(default=1, ge=0, le=1)


class WarmGlobalRequest(_BrokerRequest):
    """Global warm: invoke the cross-inbox aggregators
    (`most_active_subsystems_global` + sitemap index/meta when
    `SITE_BASE_URL` is configured). MUST be invoked after every
    `warm_inbox` job in a given cycle has completed, otherwise
    the aggregator races a warm-worker still mid-compute on its
    inbox's per-inbox key. The CLI dispatcher handles this
    sequencing automatically; ad-hoc callers should issue
    warm_global only after their warm_inbox fan-out drains.

    `targets` (Task 5 of the fast/slow tier split, spec §2)
    narrows the global aggregator set to a labelled subset,
    mirroring `WarmInboxRequest.targets`. None = run every
    global aggregator (today's shape). `--tier fast` dispatches
    no global targets at all: the sitemap aggregators it used to
    carry moved to the slow tier, whose hourly cadence is all a
    date-grained `<lastmod>` can express. `--tier slow` covers
    `sitemap:index`, `sitemap:meta`, `sitemap:maintainers` and the
    heavy `most_active_subsystems_global` query.

    `priority` mirrors `WarmInboxRequest.priority`: 0 = fast,
    1 = slow (default).
    """

    op: Literal["warm_global"] = "warm_global"
    timeout: ClassVar[float] = 300.0
    targets: list[str] | None = None
    priority: int = Field(default=1, ge=0, le=1)


# Tagged union over all valid request ops. Discriminated on `op` so
# pydantic dispatches to the right model on parse; an unknown `op`
# raises `ValidationError` at the broker boundary, which the
# handlers module turns into `Reply(ok=False, error=...)`.
Request = (
    CacheSetRequest
    | CacheDeleteRequest
    | CacheDeleteForInboxRequest
    | CachePurgeExpiredRequest
    | PingRequest
    | BootstrapInboxesRequest
    | IngestInboxRequest
    | BackfillArticleFilesRequest
    | BackfillArticleTrailersRequest
    | BackfillPatchSeriesRequest
    | BackfillCanonicalsRequest
    | UpdateMainlineRequest
    | AnalyzeRequest
    | VacuumRequest
    | FailuresReplayRequest
    | InboxCreateRequest
    | InboxUpdateRequest
    | InboxDeleteRequest
    | InboxSetTrackedAuthorsRequest
    | RobotsAddRequest
    | RobotsUpdateRequest
    | RobotsRemoveRequest
    | RobotsResetRequest
    | WarmInboxRequest
    | WarmSubsystemRequest
    | WarmGlobalRequest
)


class Reply(BaseModel):
    # Correlation ID echoed back from the matching request. Lets
    # the BrokerClient demux multiple in-flight RPCs on one socket
    # (3.0.0 wire-protocol change). Always present; the broker
    # dispatcher attaches it before sending. For Replies emitted
    # in response to malformed requests (no parseable rpc_id), the
    # broker uses 0; the client drops such replies on lookup miss.
    rpc_id: int = Field(ge=0)
    ok: bool
    # Free-form error tag on failure (e.g. "InvalidRequest",
    # "OperationalError"). Absent on success.
    error: str | None = None
    # Free-form result payload for ops that return a value:
    #   - `cache_delete`, `cache_delete_for_inbox`,
    #     `cache_purge_expired`: `{"rows_deleted": int}`
    #   - `bootstrap_inboxes`: `{"inboxes": 5}`
    #   - `ingest_inbox` (Phase 2.1): `{"results": [<IngestResult>, ...]}`
    #   - `backfill_*` (Phase 2.2):
    #       `{"counters": <BackfillResult>, "partial": bool,
    #         "run": {"id": int, "started_at": str,
    #                 "cursor": <int | None>, "examined": int,
    #                 "finished": bool, "updated_at": str} | None}`
    #     where `run.cursor` is where this chunk started and
    #     `run.examined` counts the whole run. A first chunk that
    #     discarded an unfinished run adds `run.discarded`, that run's
    #     own `run` fields. A `restart` refused for progress replies
    #     with the refused run's fields and `run.refused: true`; a
    #     `start` that finds the run live replies with its fields,
    #     `run.busy: true` and `run.retry_after` (seconds).
    # Stays `None` for ops that don't return a value.
    result: dict[str, Any] | None = None
