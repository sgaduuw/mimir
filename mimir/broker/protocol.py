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
"""
from typing import Literal, Union

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
