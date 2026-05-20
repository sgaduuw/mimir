"""RPC dispatch table and the queue-routing decision for the
write-broker. Two-file family layout (cache + longops) so the
short and long handler concerns stay separate as Phase 2.x adds
more ops:

- `cache.py`: sub-ms cache ops + ping → route to the cache worker.
- `longops.py`: ingest, backfills, mainline, analyze, vacuum,
  bootstrap_inboxes → route to the long worker.

This module owns the two cross-cutting things that have to stay in
sync with each other:

- `LONG_OPS`: the set of op names that route to the long queue.
  The reader thread (`server._reader_loop`) consults this on every
  incoming line.
- `_DISPATCH`: op name → (request model, handler) lookup, used by
  the worker to validate-and-dispatch.

`classify_op` + `dispatch` are the public functions the server
imports. Anything else in this package is implementation detail.

Errors are caught at the dispatch boundary: pydantic validation
failures become `Reply(ok=False, error="InvalidRequest")`,
SQLAlchemy `OperationalError`s become
`Reply(ok=False, error="OperationalError")`. The connection is
returned to the pool either way.
"""
import json
import logging
from typing import Callable

from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from mimir.broker.handlers.cache import (
    handle_cache_delete,
    handle_cache_delete_for_inbox,
    handle_cache_purge_expired,
    handle_cache_set,
    handle_ping,
)
from mimir.broker.handlers.longops import (
    handle_bootstrap_inboxes,
    handle_ingest_inbox,
)
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

logger = logging.getLogger(__name__)


# Long ops route to the broker's long-op worker. Keep this set
# in lockstep with `_DISPATCH` below; the reader uses this for
# routing (no DB or model side effects in the classification
# path), the worker uses `_DISPATCH` for the actual dispatch.
LONG_OPS: frozenset[str] = frozenset({
    "bootstrap_inboxes",
    "ingest_inbox",
})


# Op-name → (request model, handler). Lookup at dispatch time;
# unknown ops become `Reply(ok=False, error="UnknownOp")` rather
# than crashing the connection-handler thread.
_DISPATCH: dict[str, tuple[type, Callable]] = {
    "cache_set": (CacheSetRequest, handle_cache_set),
    "cache_delete": (CacheDeleteRequest, handle_cache_delete),
    "cache_delete_for_inbox": (
        CacheDeleteForInboxRequest, handle_cache_delete_for_inbox,
    ),
    "cache_purge_expired": (
        CachePurgeExpiredRequest, handle_cache_purge_expired,
    ),
    "ping": (PingRequest, handle_ping),
    "bootstrap_inboxes": (BootstrapInboxesRequest, handle_bootstrap_inboxes),
    "ingest_inbox": (IngestInboxRequest, handle_ingest_inbox),
}


def classify_op(line: bytes) -> str | None:
    """Cheap pre-dispatch peek at the op name so the reader thread
    can route the request onto either the cache queue or the long-
    op queue without running the full dispatch machinery. Returns
    the op string on success, `None` if the line isn't JSON or has
    no usable `op` field (in which case the reader just routes to
    the cache queue and lets `dispatch` produce a structured
    failure reply, preserving existing error semantics).

    The reader will call this once per request and stash the
    result alongside the line; the worker still calls `dispatch`,
    which re-parses (cheap on a small line) and re-validates."""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    op = raw.get("op")
    return op if isinstance(op, str) else None


def dispatch(line: bytes) -> Reply:
    """Parse one JSONL request line, dispatch to the matching
    handler, return a `Reply`. Never raises: any error becomes a
    structured failure reply so the connection stays open for the
    next request."""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        logger.warning("broker: malformed JSON: %s", exc)
        return Reply(ok=False, error="MalformedJSON")
    op = raw.get("op") if isinstance(raw, dict) else None
    entry = _DISPATCH.get(op) if isinstance(op, str) else None
    if entry is None:
        return Reply(ok=False, error="UnknownOp")
    model, handler = entry
    try:
        req = model.model_validate(raw)
    except ValidationError as exc:
        logger.warning("broker: invalid %s: %s", op, exc)
        return Reply(ok=False, error="InvalidRequest")
    try:
        return handler(req)
    except OperationalError as exc:
        logger.warning("broker: SQLite error on %s: %s", op, exc)
        return Reply(ok=False, error="OperationalError")
    except Exception as exc:
        logger.exception("broker: handler crashed on %s: %s", op, exc)
        return Reply(ok=False, error="HandlerCrashed")


__all__ = ["LONG_OPS", "classify_op", "dispatch"]
