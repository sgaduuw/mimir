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


class CacheSetRequest(BaseModel):
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


class CacheDeleteRequest(BaseModel):
    op: Literal["cache_delete"] = "cache_delete"
    key: str = Field(min_length=1, max_length=512)


class CacheDeleteForInboxRequest(BaseModel):
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


class CachePurgeExpiredRequest(BaseModel):
    op: Literal["cache_purge_expired"] = "cache_purge_expired"


class PingRequest(BaseModel):
    op: Literal["ping"] = "ping"


class IngestInboxRequest(BaseModel):
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


class BootstrapInboxesRequest(BaseModel):
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


class BackfillArticleFilesRequest(BaseModel):
    op: Literal["backfill_article_files"] = "backfill_article_files"
    limit: int | None = Field(default=None, ge=0)
    reprocess: bool = False
    continuation: int | None = Field(default=None, ge=0)


class BackfillArticleTrailersRequest(BaseModel):
    op: Literal["backfill_article_trailers"] = "backfill_article_trailers"
    limit: int | None = Field(default=None, ge=0)
    reprocess: bool = False
    continuation: int | None = Field(default=None, ge=0)


class BackfillPatchSeriesRequest(BaseModel):
    op: Literal["backfill_patch_series"] = "backfill_patch_series"
    limit: int | None = Field(default=None, ge=0)
    reprocess: bool = False
    continuation: int | None = Field(default=None, ge=0)


class BackfillCanonicalsRequest(BaseModel):
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


class UpdateMainlineRequest(BaseModel):
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


class AnalyzeRequest(BaseModel):
    """Run `ANALYZE` on the broker. `full=True` overrides the
    per-connection `analysis_limit` for this pass (no cap) so the
    weekly safety-net catches index distributions the daily bounded
    pass undersamples."""

    op: Literal["analyze"] = "analyze"
    full: bool = False


class VacuumRequest(BaseModel):
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


class WarmInboxRequest(BaseModel):
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
    """

    op: Literal["warm_inbox"] = "warm_inbox"
    inbox_name: str = Field(min_length=1, max_length=64)
    targets: list[str] | None = None


class WarmGlobalRequest(BaseModel):
    """Global warm: invoke the cross-inbox aggregators
    (`most_active_subsystems_global` + sitemap index/meta when
    `SITE_BASE_URL` is configured). MUST be invoked after every
    `warm_inbox` job in a given cycle has completed, otherwise
    the aggregator races a warm-worker still mid-compute on its
    inbox's per-inbox key. The CLI dispatcher handles this
    sequencing automatically; ad-hoc callers should issue
    warm_global only after their warm_inbox fan-out drains.
    """

    op: Literal["warm_global"] = "warm_global"


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
    WarmInboxRequest,
    WarmGlobalRequest,
]


class Reply(BaseModel):
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
