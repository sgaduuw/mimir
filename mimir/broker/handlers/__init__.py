"""RPC dispatch table and the queue-routing decision for the
write-broker. Three-file family layout (cache + longops + warm)
so each handler concern stays in its own module as Phase 2.x adds
more ops:

- `cache.py`: sub-ms cache ops + ping → route to the cache worker.
- `longops.py`: ingest, backfills, mainline, analyze, vacuum,
  bootstrap_inboxes → route to the long worker.
- `warm.py`: per-inbox + global cache warming → route to the warm
  workers (N-multi-worker queue, Phase 2.2).

This module owns one table, `_HANDLERS`: one row per op of
(request model, handler, queue). Everything the server routes on is
derived from it:

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
from collections.abc import Callable

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
    handle_reindex,
)
from mimir.broker.handlers.maintenance import (
    handle_analyze,
    handle_backfill_thread_roots,
    handle_failures_replay,
    handle_inbox_create,
    handle_inbox_delete,
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
    BackfillThreadRootsRequest,
    BootstrapInboxesRequest,
    CacheDeleteForInboxRequest,
    CacheDeleteRequest,
    CachePurgeExpiredRequest,
    CacheSetRequest,
    FailuresReplayRequest,
    InboxCreateRequest,
    InboxDeleteRequest,
    InboxSetTrackedAuthorsRequest,
    InboxUpdateRequest,
    IngestInboxRequest,
    PingRequest,
    ReindexRequest,
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


# One row per op: (request model, handler, queue). The op name is
# the model's `op` default, so it is spelled once, in protocol.py;
# the routing sets and the dispatch lookup below are derived, which
# is what keeps them from drifting apart. The queue is "cache"
# (sub-ms commits, the only queue the web tier waits on), "long"
# (minutes-long ops on one worker) or "warm" (cache warming, drained
# by N parallel workers). An op belongs to exactly one.
_HANDLERS: list[tuple[type, Callable, str]] = [
    (CacheSetRequest, handle_cache_set, "cache"),
    (CacheDeleteRequest, handle_cache_delete, "cache"),
    (CacheDeleteForInboxRequest, handle_cache_delete_for_inbox, "cache"),
    (CachePurgeExpiredRequest, handle_cache_purge_expired, "cache"),
    (PingRequest, handle_ping, "cache"),
    (BootstrapInboxesRequest, handle_bootstrap_inboxes, "long"),
    (IngestInboxRequest, handle_ingest_inbox, "long"),
    (ReindexRequest, handle_reindex, "long"),
    (BackfillArticleFilesRequest, handle_backfill_article_files, "long"),
    (BackfillArticleTrailersRequest, handle_backfill_article_trailers, "long"),
    (BackfillPatchSeriesRequest, handle_backfill_patch_series, "long"),
    (BackfillCanonicalsRequest, handle_backfill_canonicals, "long"),
    (UpdateMainlineRequest, handle_update_mainline, "long"),
    (AnalyzeRequest, handle_analyze, "long"),
    (BackfillThreadRootsRequest, handle_backfill_thread_roots, "long"),
    (VacuumRequest, handle_vacuum, "long"),
    (FailuresReplayRequest, handle_failures_replay, "long"),
    (InboxCreateRequest, handle_inbox_create, "long"),
    (InboxUpdateRequest, handle_inbox_update, "long"),
    (InboxDeleteRequest, handle_inbox_delete, "long"),
    (InboxSetTrackedAuthorsRequest, handle_inbox_set_tracked_authors, "long"),
    (RobotsAddRequest, handle_robots_add, "long"),
    (RobotsUpdateRequest, handle_robots_update, "long"),
    (RobotsRemoveRequest, handle_robots_remove, "long"),
    (RobotsResetRequest, handle_robots_reset, "long"),
    (WarmInboxRequest, handle_warm_inbox, "warm"),
    (WarmSubsystemRequest, handle_warm_subsystem, "warm"),
    (WarmGlobalRequest, handle_warm_global, "warm"),
]


def _op_name(model: type) -> str:
    return model.model_fields["op"].default


# The reader routes on these two sets (a cheap JSON peek, no model
# side effects); anything in neither falls through to the cache queue.
LONG_OPS: frozenset[str] = frozenset(
    _op_name(m) for m, _h, queue in _HANDLERS if queue == "long"
)
WARM_OPS: frozenset[str] = frozenset(
    _op_name(m) for m, _h, queue in _HANDLERS if queue == "warm"
)

# Op-name → (request model, handler). Lookup at dispatch time;
# unknown ops become `Reply(ok=False, error="UnknownOp")` rather
# than crashing the connection-handler thread.
_DISPATCH: dict[str, tuple[type, Callable]] = {
    _op_name(m): (m, h) for m, h, _queue in _HANDLERS
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
    pre_parse_rpc_id = (
        raw_rpc_id if isinstance(raw_rpc_id, int) and raw_rpc_id >= 0 else 0
    )
    entry = _DISPATCH.get(op) if isinstance(op, str) else None
    if entry is None:
        return Reply(rpc_id=pre_parse_rpc_id, ok=False, error="UnknownOp")
    model, handler = entry
    # The model defaults rpc_id for client-side construction; the wire
    # still requires the key, so a request without it never runs.
    if "rpc_id" not in raw:
        logger.warning("broker: %s without rpc_id", op)
        return Reply(rpc_id=0, ok=False, error="InvalidRequest")
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
