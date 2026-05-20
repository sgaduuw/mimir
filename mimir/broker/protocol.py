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
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, field_validator


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
    # Free-form result payload for long ops: e.g.
    # `{"inboxes": 5}` from `bootstrap_inboxes`, or (in Phase 2.1)
    # `{"new": N, "linked": N, ...}` from `ingest_epoch`. Stays
    # `None` for ops that don't return a value.
    result: dict[str, Any] | None = None
