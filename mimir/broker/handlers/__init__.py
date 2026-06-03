"""RPC dispatch table and the queue-routing decision for the
write-broker. Three-file family layout (cache + longops + warm)
so each handler concern stays in its own module as Phase 2.x adds
more ops:

- `cache.py`: sub-ms cache ops + ping → route to the cache worker.
- `longops.py`: ingest, backfills, mainline, analyze, vacuum,
  bootstrap_inboxes → route to the long worker.
- `warm.py`: per-inbox + global cache warming → route to the warm
  workers (N-multi-worker queue, Phase 2.2).

This module owns the three cross-cutting things that have to stay
in sync with each other:

- `LONG_OPS`: op names that route to the long queue.
- `WARM_OPS`: op names that route to the warm queue (Phase 2.2).
- `_DISPATCH`: op name → (request model, handler) lookup, used by
  the worker to validate-and-dispatch.

The reader thread (`server._reader_loop`) consults `LONG_OPS` and
`WARM_OPS` to route; anything else falls to the cache queue.
`classify_op` + `dispatch` are the public functions the server
imports.

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
    handle_backfill_article_files,
    handle_backfill_article_trailers,
    handle_backfill_canonicals,
    handle_backfill_patch_series,
    handle_bootstrap_inboxes,
    handle_ingest_inbox,
)
from mimir.broker.handlers.maintenance import (
    handle_analyze,
    handle_failures_replay,
    handle_inbox_add_tracked_author,
    handle_inbox_clear_tracked_authors,
    handle_inbox_create,
    handle_inbox_delete,
    handle_inbox_remove_tracked_author,
    handle_inbox_set_tracked_authors,
    handle_inbox_update,
    handle_robots_add,
    handle_robots_remove,
    handle_robots_reset,
    handle_robots_update,
    handle_update_mainline,
    handle_vacuum,
)
from mimir.broker.handlers.warm import (
    handle_warm_global,
    handle_warm_inbox,
    handle_warm_subsystem,
)
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
)

logger = logging.getLogger(__name__)


# Long ops route to the broker's long-op worker. Keep this set
# in lockstep with `_DISPATCH` below; the reader uses this for
# routing (no DB or model side effects in the classification
# path), the worker uses `_DISPATCH` for the actual dispatch.
LONG_OPS: frozenset[str] = frozenset(
    {
        "bootstrap_inboxes",
        "ingest_inbox",
        "backfill_article_files",
        "backfill_article_trailers",
        "backfill_patch_series",
        "backfill_canonicals",
        "update_mainline",
        "analyze",
        "vacuum",
        "failures_replay",
        "inbox_create",
        "inbox_update",
        "inbox_delete",
        "inbox_set_tracked_authors",
        "inbox_add_tracked_author",
        "inbox_remove_tracked_author",
        "inbox_clear_tracked_authors",
        "robots_add",
        "robots_update",
        "robots_remove",
        "robots_reset",
    }
)


# Warm ops route to the broker's warm queue, which is drained by
# N parallel warm-workers (Phase 2.2). Sibling to LONG_OPS; an op
# can only belong to one routing set. Anything in neither set
# falls through to the cache queue.
WARM_OPS: frozenset[str] = frozenset(
    {
        "warm_inbox",
        "warm_subsystem",
        "warm_global",
    }
)


# Op-name → (request model, handler). Lookup at dispatch time;
# unknown ops become `Reply(ok=False, error="UnknownOp")` rather
# than crashing the connection-handler thread.
_DISPATCH: dict[str, tuple[type, Callable]] = {
    "cache_set": (CacheSetRequest, handle_cache_set),
    "cache_delete": (CacheDeleteRequest, handle_cache_delete),
    "cache_delete_for_inbox": (
        CacheDeleteForInboxRequest,
        handle_cache_delete_for_inbox,
    ),
    "cache_purge_expired": (
        CachePurgeExpiredRequest,
        handle_cache_purge_expired,
    ),
    "ping": (PingRequest, handle_ping),
    "bootstrap_inboxes": (BootstrapInboxesRequest, handle_bootstrap_inboxes),
    "ingest_inbox": (IngestInboxRequest, handle_ingest_inbox),
    "backfill_article_files": (
        BackfillArticleFilesRequest,
        handle_backfill_article_files,
    ),
    "backfill_article_trailers": (
        BackfillArticleTrailersRequest,
        handle_backfill_article_trailers,
    ),
    "backfill_patch_series": (
        BackfillPatchSeriesRequest,
        handle_backfill_patch_series,
    ),
    "backfill_canonicals": (
        BackfillCanonicalsRequest,
        handle_backfill_canonicals,
    ),
    "update_mainline": (UpdateMainlineRequest, handle_update_mainline),
    "analyze": (AnalyzeRequest, handle_analyze),
    "vacuum": (VacuumRequest, handle_vacuum),
    "failures_replay": (FailuresReplayRequest, handle_failures_replay),
    "inbox_create": (InboxCreateRequest, handle_inbox_create),
    "inbox_update": (InboxUpdateRequest, handle_inbox_update),
    "inbox_delete": (InboxDeleteRequest, handle_inbox_delete),
    "inbox_set_tracked_authors": (
        InboxSetTrackedAuthorsRequest,
        handle_inbox_set_tracked_authors,
    ),
    "inbox_add_tracked_author": (
        InboxAddTrackedAuthorRequest,
        handle_inbox_add_tracked_author,
    ),
    "inbox_remove_tracked_author": (
        InboxRemoveTrackedAuthorRequest,
        handle_inbox_remove_tracked_author,
    ),
    "inbox_clear_tracked_authors": (
        InboxClearTrackedAuthorsRequest,
        handle_inbox_clear_tracked_authors,
    ),
    "robots_add": (RobotsAddRequest, handle_robots_add),
    "robots_update": (RobotsUpdateRequest, handle_robots_update),
    "robots_remove": (RobotsRemoveRequest, handle_robots_remove),
    "robots_reset": (RobotsResetRequest, handle_robots_reset),
    "warm_inbox": (WarmInboxRequest, handle_warm_inbox),
    "warm_subsystem": (WarmSubsystemRequest, handle_warm_subsystem),
    "warm_global": (WarmGlobalRequest, handle_warm_global),
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
    next request.

    For error replies produced before a valid request is parsed
    (MalformedJSON, UnknownOp, InvalidRequest) the rpc_id cannot
    be recovered; the spec mandates rpc_id=0 in that case. The
    client treats a lookup miss on rpc_id=0 as an ignorable stray
    error frame (no in-flight RPC has id 0; valid ids start at 1).
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        logger.warning("broker: malformed JSON: %s", exc)
        return Reply(rpc_id=0, ok=False, error="MalformedJSON")
    op = raw.get("op") if isinstance(raw, dict) else None
    # Best-effort rpc_id extraction for pre-parse error replies.
    # If the wire message had a numeric rpc_id field we echo it back
    # even when we can't fully validate the request, so a pipelining
    # client can correlate the error to its in-flight slot.
    raw_rpc_id = raw.get("rpc_id") if isinstance(raw, dict) else None
    pre_parse_rpc_id = raw_rpc_id if isinstance(raw_rpc_id, int) and raw_rpc_id >= 0 else 0
    entry = _DISPATCH.get(op) if isinstance(op, str) else None
    if entry is None:
        return Reply(rpc_id=pre_parse_rpc_id, ok=False, error="UnknownOp")
    model, handler = entry
    try:
        req = model.model_validate(raw)
    except ValidationError as exc:
        logger.warning("broker: invalid %s: %s", op, exc)
        return Reply(rpc_id=pre_parse_rpc_id, ok=False, error="InvalidRequest")
    try:
        return handler(req)
    except OperationalError as exc:
        logger.warning("broker: SQLite error on %s: %s", op, exc)
        return Reply(rpc_id=req.rpc_id, ok=False, error="OperationalError")
    except Exception as exc:
        logger.exception("broker: handler crashed on %s: %s", op, exc)
        return Reply(rpc_id=req.rpc_id, ok=False, error="HandlerCrashed")


__all__ = ["LONG_OPS", "WARM_OPS", "classify_op", "dispatch"]
