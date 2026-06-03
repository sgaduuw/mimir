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
seconds to minutes; `bootstrap_inboxes` and the Phase 2.1+ additions
to come: `ingest_epoch`, `backfill_*`, `update_mainline`, `analyze`,
`vacuum`) route to the broker's long worker. The two workers compete
for the SQLite writer lock at the SQLite level, so cache writes only
wait for the long worker's current commit batch, not the whole long
op. See `handlers.LONG_OPS` for the routing set.
"""

import re
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, field_validator


# Mirror of `mimir.inboxes._NAME_RE`. Duplicated here (rather than
# imported) so the broker protocol module stays free of the heavy
# inboxes/cache/extensions import chain that an `inboxes` import
# would drag in. The two regexes must stay in sync; the inboxes-side
# validator is the canonical source and any change there should
# update this one in lockstep.
_INBOX_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def _validate_inbox_name(v: str) -> str:
    """Shared slug-regex validator used by every request type that
    carries an `Inbox.name`-shaped field. Re-enforcing slug shape
    at the wire boundary keeps a buggy or hostile peer from
    smuggling LIKE metacharacters / path components / weird casing
    into ops that compose the value into SQL or filesystem paths."""
    if not _INBOX_NAME_RE.fullmatch(v):
        raise ValueError(
            "name must be lowercase alphanumeric/hyphen, "
            "1 to 64 chars, not starting or ending with a hyphen"
        )
    return v


class _BrokerRequest(BaseModel):
    """Base for every broker Request type. Carries the required
    `rpc_id` field that the client allocates and the broker echoes
    back into the Reply so the client can demux concurrent in-flight
    RPCs on one socket. 3.0.0 wire-protocol change."""

    rpc_id: int = Field(ge=0)


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
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, v: str) -> str:
        # `cache._direct_delete_for_inbox` translates this name into a
        # `LIKE %name%` clause. The CLI write paths slug-validate via
        # `mimir.inboxes.validate_name` before the call ever reaches
        # cache, so LIKE-pattern metacharacters can't appear in
        # practice. Re-enforcing it at the wire boundary keeps a
        # buggy or hostile peer from passing `_:foo` (or any other
        # pattern) and matching unrelated rows.
        if not _INBOX_NAME_RE.fullmatch(v):
            raise ValueError(
                "name must be lowercase alphanumeric/hyphen, "
                "1 to 64 chars, not starting or ending with a hyphen"
            )
        return v


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
    inbox_name: str = Field(min_length=1, max_length=64)
    limit: int | None = Field(default=None, ge=0)
    workers: int | None = Field(default=None, ge=1)


class BootstrapInboxesRequest(_BrokerRequest):
    """Long op: reconcile `Settings.inboxes` env config into the
    `inboxes` table. Idempotent via `ON CONFLICT (name) DO NOTHING`.
    Smallest of the long ops and the migration canary for Phase 2.0
    (proves the long-worker + per-op-timeout path end-to-end before
    Phase 2.1 migrates the meatier ingest ops)."""

    op: Literal["bootstrap_inboxes"] = "bootstrap_inboxes"


# Phase 2.2 long ops: the four backfills. Each shares the same RPC
# shape (limit + reprocess + continuation) because each one's CLI
# loop wants the same thing: feed the broker a chunk budget, get
# back per-chunk counters + a continuation pointer, repeat until
# done. `continuation` carries the last `Article.id` the prior chunk
# processed; the handler resumes at `Article.id < continuation` on
# the next call. `None` means "start at the newest article."
#
# A backfill RPC handler runs for at most `Settings.broker_backfill_
# chunk_seconds` (default 10 s) before returning `partial=True,
# continuation=<last id>`. Between chunks the broker's long-op
# worker is free, queued cache writes and other long ops get to
# run, then the CLI fires the next chunk. Multi-hour backfills no
# longer monopolise the long worker.


class BackfillArticleFilesRequest(_BrokerRequest):
    op: Literal["backfill_article_files"] = "backfill_article_files"
    limit: int | None = Field(default=None, ge=0)
    reprocess: bool = False
    continuation: int | None = Field(default=None, ge=0)


class BackfillArticleTrailersRequest(_BrokerRequest):
    op: Literal["backfill_article_trailers"] = "backfill_article_trailers"
    limit: int | None = Field(default=None, ge=0)
    reprocess: bool = False
    continuation: int | None = Field(default=None, ge=0)


class BackfillPatchSeriesRequest(_BrokerRequest):
    op: Literal["backfill_patch_series"] = "backfill_patch_series"
    limit: int | None = Field(default=None, ge=0)
    reprocess: bool = False
    continuation: int | None = Field(default=None, ge=0)


class BackfillCanonicalsRequest(_BrokerRequest):
    """Phase 2.2 long op: chunked `backfill_canonicals` over the
    broker. Same shape as the patch-metadata backfills plus
    `inbox_filter` for the `admin canonicals backfill --inbox`
    surface."""

    op: Literal["backfill_canonicals"] = "backfill_canonicals"
    inbox_filter: str | None = Field(default=None, min_length=1, max_length=64)
    limit: int | None = Field(default=None, ge=0)
    reprocess: bool = False
    continuation: int | None = Field(default=None, ge=0)


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
    skip_fetch: bool = False
    skip_maintainers: bool = False
    skip_commits: bool = False
    force: bool = False


class AnalyzeRequest(_BrokerRequest):
    """Run `ANALYZE` on the broker. `full=True` overrides the
    per-connection `analysis_limit` for this pass (no cap) so the
    weekly safety-net catches index distributions the daily bounded
    pass undersamples."""

    op: Literal["analyze"] = "analyze"
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
    inbox_name: str = Field(min_length=1, max_length=64)
    epoch_filter: str | None = Field(default=None, min_length=1, max_length=16)
    limit: int | None = Field(default=None, ge=0)

    @field_validator("inbox_name")
    @classmethod
    def _name_is_slug(cls, v: str) -> str:
        return _validate_inbox_name(v)


# Phase 2.4 admin-inbox CRUD ops. Each one is a split RPC matching
# the same shape as the rest of the broker family: one pydantic
# type per logical op, dispatched to a per-op handler. All seven
# route to the long queue alongside `bootstrap_inboxes` (sub-second
# writes, but conservative routing keeps them out of cache-queue
# contention). Name validation uses the shared
# `_validate_inbox_name` defined near `_INBOX_NAME_RE` at module
# top.


class InboxCreateRequest(_BrokerRequest):
    """Insert a new Inbox after validating all three fields. Handler
    delegates to `mimir.inboxes.create_inbox`."""

    op: Literal["inbox_create"] = "inbox_create"
    name: str = Field(min_length=1, max_length=64)
    mirror_path: str = Field(min_length=1, max_length=512)
    upstream_url: str = Field(min_length=1, max_length=512)

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, v: str) -> str:
        return _validate_inbox_name(v)


class InboxUpdateRequest(_BrokerRequest):
    """Modify an existing inbox. Only the supplied fields are
    touched server-side. Handler delegates to
    `mimir.inboxes.update_inbox`."""

    op: Literal["inbox_update"] = "inbox_update"
    name: str = Field(min_length=1, max_length=64)
    new_name: str | None = Field(default=None, min_length=1, max_length=64)
    mirror_path: str | None = Field(default=None, min_length=1, max_length=512)
    upstream_url: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("name", "new_name")
    @classmethod
    def _name_is_slug(cls, v: str | None) -> str | None:
        return None if v is None else _validate_inbox_name(v)


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
    name: str = Field(min_length=1, max_length=64)
    keep_orphan_articles: bool = False
    remove_inbox_data: bool = False

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, v: str) -> str:
        return _validate_inbox_name(v)


class InboxSetTrackedAuthorsRequest(_BrokerRequest):
    """Replace the per-inbox tracker dict in one shot. Handler
    delegates to `mimir.inboxes.set_tracked_authors`."""

    op: Literal["inbox_set_tracked_authors"] = "inbox_set_tracked_authors"
    name: str = Field(min_length=1, max_length=64)
    trackers: dict[str, str]

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, v: str) -> str:
        return _validate_inbox_name(v)


class InboxAddTrackedAuthorRequest(_BrokerRequest):
    """Add (or replace) one tracker entry. Handler delegates to
    `mimir.inboxes.add_tracked_author`."""

    op: Literal["inbox_add_tracked_author"] = "inbox_add_tracked_author"
    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    substring: str = Field(min_length=1, max_length=512)

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, v: str) -> str:
        return _validate_inbox_name(v)


class InboxRemoveTrackedAuthorRequest(_BrokerRequest):
    """Remove one tracker entry by label. Handler delegates to
    `mimir.inboxes.remove_tracked_author`."""

    op: Literal["inbox_remove_tracked_author"] = "inbox_remove_tracked_author"
    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, v: str) -> str:
        return _validate_inbox_name(v)


class InboxClearTrackedAuthorsRequest(_BrokerRequest):
    """Drop all tracker entries (writes NULL). Handler delegates to
    `mimir.inboxes.clear_tracked_authors`."""

    op: Literal["inbox_clear_tracked_authors"] = "inbox_clear_tracked_authors"
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, v: str) -> str:
        return _validate_inbox_name(v)


# Mirror of `mimir.robots._UA_RE` / `_PATH_RE` / Content-Signal
# enums. Duplicated here (rather than imported) so the protocol
# module stays free of the heavy `mimir.robots` → `mimir.extensions`
# import chain. The robots-side validator is the canonical source;
# any change there should update these in lockstep.
_ROBOTS_UA_RE = re.compile(r"^[\x21-\x7e]{1,64}$")
_ROBOTS_PATH_RE = re.compile(r"^[/*][\x21-\x7e]{0,255}$")
_ROBOTS_CS_KEYS = frozenset({"search", "ai-input", "ai-train"})
_ROBOTS_CS_VALUES = frozenset({"yes", "no"})


def _validate_robots_user_agent(v: str) -> str:
    if not _ROBOTS_UA_RE.fullmatch(v):
        raise ValueError(
            "user_agent must be 1-64 visible-ASCII characters with no whitespace"
        )
    return v


def _validate_robots_paths(v: list[str]) -> list[str]:
    for p in v:
        if not _ROBOTS_PATH_RE.fullmatch(p):
            raise ValueError(
                f"disallow path {p!r} must start with '/' or '*', "
                "1-256 visible-ASCII characters, no whitespace"
            )
    return v


def _validate_robots_content_signals(v: dict[str, str]) -> dict[str, str]:
    for k, val in v.items():
        if k not in _ROBOTS_CS_KEYS:
            raise ValueError(
                f"content_signal key {k!r} must be one of {sorted(_ROBOTS_CS_KEYS)}"
            )
        if val not in _ROBOTS_CS_VALUES:
            raise ValueError(
                f"content_signal value {val!r} must be one of "
                f"{sorted(_ROBOTS_CS_VALUES)}"
            )
    return v


def _validate_robots_content_signal_keys(v: list[str]) -> list[str]:
    for k in v:
        if k not in _ROBOTS_CS_KEYS:
            raise ValueError(
                f"content_signal key {k!r} must be one of {sorted(_ROBOTS_CS_KEYS)}"
            )
    return v


class RobotsAddRequest(_BrokerRequest):
    """Insert one new robots_rules row. Handler delegates to
    `mimir.robots.add_rule`."""

    op: Literal["robots_add"] = "robots_add"
    user_agent: str = Field(min_length=1, max_length=64)
    disallow: list[str] = Field(default_factory=list, max_length=64)
    crawl_delay: int | None = Field(default=None, ge=0, le=86_400)
    content_signals: dict[str, str] = Field(default_factory=dict)

    @field_validator("user_agent")
    @classmethod
    def _ua_ok(cls, v: str) -> str:
        return _validate_robots_user_agent(v)

    @field_validator("disallow")
    @classmethod
    def _paths_ok(cls, v: list[str]) -> list[str]:
        return _validate_robots_paths(v)

    @field_validator("content_signals")
    @classmethod
    def _signals_ok(cls, v: dict[str, str]) -> dict[str, str]:
        return _validate_robots_content_signals(v)


class RobotsUpdateRequest(_BrokerRequest):
    """Mutate one existing robots_rules row. Handler delegates to
    `mimir.robots.update_rule`. `clear_crawl_delay=True` sets the
    column to NULL (distinct from omitting `crawl_delay` which means
    "don't touch"). Content-Signal mutations parallel disallow:
    `set_content_signal` upserts; `clear_content_signal` (list of
    keys) removes; `clear_all_content_signals` wipes to NULL."""

    op: Literal["robots_update"] = "robots_update"
    user_agent: str = Field(min_length=1, max_length=64)
    add_disallow: list[str] = Field(default_factory=list, max_length=64)
    remove_disallow: list[str] = Field(default_factory=list, max_length=64)
    crawl_delay: int | None = Field(default=None, ge=0, le=86_400)
    clear_crawl_delay: bool = False
    set_content_signal: dict[str, str] = Field(default_factory=dict)
    clear_content_signal: list[str] = Field(default_factory=list, max_length=8)
    clear_all_content_signals: bool = False

    @field_validator("user_agent")
    @classmethod
    def _ua_ok(cls, v: str) -> str:
        return _validate_robots_user_agent(v)

    @field_validator("add_disallow", "remove_disallow")
    @classmethod
    def _paths_ok(cls, v: list[str]) -> list[str]:
        return _validate_robots_paths(v)

    @field_validator("set_content_signal")
    @classmethod
    def _set_signals_ok(cls, v: dict[str, str]) -> dict[str, str]:
        return _validate_robots_content_signals(v)

    @field_validator("clear_content_signal")
    @classmethod
    def _clear_signal_keys_ok(cls, v: list[str]) -> list[str]:
        return _validate_robots_content_signal_keys(v)


class RobotsRemoveRequest(_BrokerRequest):
    """Drop one robots_rules row. `*` is refused at the service
    layer. Handler delegates to `mimir.robots.remove_rule`."""

    op: Literal["robots_remove"] = "robots_remove"
    user_agent: str = Field(min_length=1, max_length=64)

    @field_validator("user_agent")
    @classmethod
    def _ua_ok(cls, v: str) -> str:
        return _validate_robots_user_agent(v)


class RobotsResetRequest(_BrokerRequest):
    """Drop every row and re-seed the `*` stanza with the migration's
    defaults. Handler delegates to `mimir.robots.reset_rules`."""

    op: Literal["robots_reset"] = "robots_reset"


class VacuumRequest(_BrokerRequest):
    """Run `VACUUM` (+ WAL checkpoint) on the broker. Holds the
    SQLite exclusive lock for the duration; cache writes from the
    web tier queue behind it and may time out under the broker
    client's per-RPC ceiling on huge databases. Documented in
    CONTEXT.md as an accepted weekly-quiet-window trade-off."""

    op: Literal["vacuum"] = "vacuum"


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
    inbox_name: str = Field(min_length=1, max_length=64)
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
    inbox_name: str = Field(min_length=1, max_length=64)
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
    global aggregator (today's shape). The CLI's
    `--tier fast` dispatches with `targets=["sitemap:index",
    "sitemap:meta"]` so the per-minute scheduler tick only
    refreshes the cheap sitemap-index aggregators; `--tier slow`
    narrows to the heavy `most_active_subsystems_global` query.

    `priority` mirrors `WarmInboxRequest.priority`: 0 = fast,
    1 = slow (default).
    """

    op: Literal["warm_global"] = "warm_global"
    targets: list[str] | None = None
    priority: int = Field(default=1, ge=0, le=1)


# Tagged union over all valid request ops. Discriminated on `op` so
# pydantic dispatches to the right model on parse; an unknown `op`
# raises `ValidationError` at the broker boundary, which the
# handlers module turns into `Reply(ok=False, error=...)`.
Request = Union[
    CacheSetRequest,
    CacheDeleteRequest,
    CacheDeleteForInboxRequest,
    CachePurgeExpiredRequest,
    PingRequest,
    BootstrapInboxesRequest,
    IngestInboxRequest,
    BackfillArticleFilesRequest,
    BackfillArticleTrailersRequest,
    BackfillPatchSeriesRequest,
    BackfillCanonicalsRequest,
    UpdateMainlineRequest,
    AnalyzeRequest,
    VacuumRequest,
    FailuresReplayRequest,
    InboxCreateRequest,
    InboxUpdateRequest,
    InboxDeleteRequest,
    InboxSetTrackedAuthorsRequest,
    InboxAddTrackedAuthorRequest,
    InboxRemoveTrackedAuthorRequest,
    InboxClearTrackedAuthorsRequest,
    RobotsAddRequest,
    RobotsUpdateRequest,
    RobotsRemoveRequest,
    RobotsResetRequest,
    WarmInboxRequest,
    WarmSubsystemRequest,
    WarmGlobalRequest,
]


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
    # Optional payload, set by ops that return a value. Today only
    # `cache_purge_expired` returns `rows_deleted`. Keeps the reply
    # shape uniform across ops; absent for ops with no return value.
    rows_deleted: int | None = None
    # Free-form result payload for long ops:
    #   - `bootstrap_inboxes`: `{"inboxes": 5}`
    #   - `ingest_inbox` (Phase 2.1): `{"results": [<IngestResult>, ...]}`
    #   - `backfill_*` (Phase 2.2):
    #       `{"counters": <BackfillResult>, "partial": bool,
    #         "continuation": <int | None>}`
    # Stays `None` for ops that don't return a value.
    result: dict[str, Any] | None = None
