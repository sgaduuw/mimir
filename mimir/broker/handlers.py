"""RPC dispatch + per-op handlers for the write-broker.

Each handler parses one `protocol` message, calls the matching
`mimir.cache._direct_<op>` helper inside a `write_transaction()`
block (BEGIN IMMEDIATE + bumped busy_timeout on the pooled writer
connection), and returns a `Reply`. Imports the `_direct_*` helpers
rather than the public functions so the broker doesn't recurse into
broker mode and infinite-loop.

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

from mimir import cache
from mimir.broker.protocol import (
    CacheDeleteForInboxRequest,
    CacheDeleteRequest,
    CachePurgeExpiredRequest,
    CacheSetRequest,
    PingRequest,
    Reply,
)
from mimir.extensions import write_transaction

logger = logging.getLogger(__name__)


def _handle_cache_set(req: CacheSetRequest) -> Reply:
    with write_transaction("broker:cache_set"):
        cache._direct_set(req.key, req.value_json, req.ttl)
    return Reply(ok=True)


def _handle_cache_delete(req: CacheDeleteRequest) -> Reply:
    with write_transaction("broker:cache_delete"):
        cache._direct_delete(req.key)
    return Reply(ok=True)


def _handle_cache_delete_for_inbox(req: CacheDeleteForInboxRequest) -> Reply:
    with write_transaction("broker:cache_delete_for_inbox"):
        n = cache._direct_delete_for_inbox(req.name)
    return Reply(ok=True, rows_deleted=n)


def _handle_cache_purge_expired(req: CachePurgeExpiredRequest) -> Reply:
    with write_transaction("broker:cache_purge_expired"):
        n = cache._direct_purge_expired()
    return Reply(ok=True, rows_deleted=n)


def _handle_ping(req: PingRequest) -> Reply:
    return Reply(ok=True)


# Op-name → (request model, handler). Lookup at dispatch time;
# unknown ops become `Reply(ok=False, error="UnknownOp")` rather
# than crashing the connection-handler thread.
_DISPATCH: dict[str, tuple[type, Callable]] = {
    "cache_set": (CacheSetRequest, _handle_cache_set),
    "cache_delete": (CacheDeleteRequest, _handle_cache_delete),
    "cache_delete_for_inbox": (
        CacheDeleteForInboxRequest, _handle_cache_delete_for_inbox,
    ),
    "cache_purge_expired": (
        CachePurgeExpiredRequest, _handle_cache_purge_expired,
    ),
    "ping": (PingRequest, _handle_ping),
}


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
