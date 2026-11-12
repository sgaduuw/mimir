"""Cache-op handlers. Each is a sub-ms operation: dispatch a
WriteOp through the active WriterThread and reply. The delete and
purge handlers await `.result()` before replying; `handle_cache_set`
replies once the write is queued (#1157).

Phase 4 of the broker two-pool restructure
(`_claude/specs/2026-05-29-broker-two-pool-design.md`): the
handlers do not write inline on the cache worker thread.
Instead, they submit single-statement WriteOps
through `_context.get_active_writer()` (all but set await `.result()`).
The cache worker thread keeps running (it dequeues from
`cache_queue` and dispatches) but the actual SQLite write
lands on the WriterThread, serialised with long-op WriteOps.
After Phase 4 every SQLite write inside the broker funnels
through one WriterThread.

`ping` lives here too: it doesn't touch the DB at all (just a
liveness probe used by the compose healthcheck) but classifies
as a cache-family op for routing purposes since it's expected
to be sub-ms.

Best-effort posture (2.15.1 hotfix): each handler catches
`TimeoutError` from `.result(timeout=5)` and returns
`Reply(ok=False, error="writer busy")` with a WARNING log line.
`handle_cache_set` is the exception (#1157): it replies once the
write is queued and never waits on the commit; a full writer queue
drops the write with a "cache_set dropped" WARNING.
The web tier's `cache.set` is already best-effort by contract
(it logs + swallows non-OK replies; the cached value still ages
out via TTL). Without this catch, a long writer-lock hold
(VACUUM in particular, ~minutes per cycle) trips
`TimeoutError` per cache RPC and `dispatch`'s outer exception
handler logs each one as an ERROR with full traceback, which
is noisy. During VACUUM, reject new cache mutations before writer submission
and count them for one maintenance summary. A delete or purge
admitted before the VACUUM gate closes can still hit the ordinary
timeout; a cache.set admitted then just waits in the writer queue.
"""

import logging
import queue
import threading

from mimir import cache
from mimir.broker._context import get_active_writer
from mimir.broker.protocol import (
    CacheDeleteForInboxRequest,
    CacheDeleteRequest,
    CachePurgeExpiredRequest,
    CacheSetRequest,
    PingRequest,
    Reply,
)

logger = logging.getLogger(__name__)

# Long-worker concurrency can overlap VACUUM handlers. One exit must not
# reopen cache admission while another VACUUM still holds SQLite's writer.
_vacuum_lock = threading.Lock()
_active_vacuums = 0
_skipped_writes = 0


def begin_vacuum() -> None:
    global _active_vacuums, _skipped_writes
    with _vacuum_lock:
        if not _active_vacuums:
            _skipped_writes = 0
        _active_vacuums += 1


def end_vacuum() -> int | None:
    """Reopen admission and return the summary count on the final exit."""
    global _active_vacuums, _skipped_writes
    with _vacuum_lock:
        _active_vacuums -= 1
        if _active_vacuums:
            return None
        skipped = _skipped_writes
        _skipped_writes = 0
        return skipped


def _skip_during_vacuum() -> bool:
    global _skipped_writes
    with _vacuum_lock:
        if not _active_vacuums:
            return False
        _skipped_writes += 1
        return True


def handle_cache_set(req: CacheSetRequest) -> Reply:
    if _skip_during_vacuum():
        return Reply(rpc_id=req.rpc_id, ok=False, error="vacuum")
    writer = get_active_writer()
    # req.key is already namespaced and req.value_json already JSON-encoded
    # (the web tier's cache.set did both, and checked the size cap, before
    # sending), hence the nskey helper. Reply once the write is queued,
    # not after the commit: a held writer (ANALYZE) must not stall the
    # viewer (#1157). A full queue drops the write rather than block.
    try:
        future = cache._set_via_writer_for_nskey(
            writer, req.key, req.value_json, req.ttl, block=False
        )
    except queue.Full:
        logger.warning("cache_set dropped: writer queue full (key=%s)", req.key)
        return Reply(rpc_id=req.rpc_id, ok=False, error="writer busy")

    # Nobody reads the future, so log a failed commit here.
    def _log_failure(f):
        if (exc := f.exception()) is not None:
            logger.warning("cache_set failed (key=%s): %s", req.key, exc)

    future.add_done_callback(_log_failure)
    return Reply(rpc_id=req.rpc_id, ok=True)


def handle_cache_delete(req: CacheDeleteRequest) -> Reply:
    if _skip_during_vacuum():
        return Reply(rpc_id=req.rpc_id, ok=False, error="vacuum")
    writer = get_active_writer()
    # req.key is pre-namespaced. We discard the rowcount; reply shape
    # matches the pre-Phase-4 Reply(ok=True) exactly.
    try:
        cache.delete_via_writer(writer, req.key).result(timeout=5)
    except TimeoutError:
        logger.warning("cache_delete timeout: writer busy (key=%s)", req.key)
        return Reply(rpc_id=req.rpc_id, ok=False, error="writer busy")
    return Reply(rpc_id=req.rpc_id, ok=True)


def handle_cache_delete_for_inbox(req: CacheDeleteForInboxRequest) -> Reply:
    if _skip_during_vacuum():
        return Reply(rpc_id=req.rpc_id, ok=False, error="vacuum")
    writer = get_active_writer()
    try:
        n = cache.delete_for_inbox_via_writer(writer, req.name).result(timeout=5)
    except TimeoutError:
        logger.warning(
            "cache_delete_for_inbox timeout: writer busy (name=%s)", req.name
        )
        return Reply(rpc_id=req.rpc_id, ok=False, error="writer busy")
    return Reply(rpc_id=req.rpc_id, ok=True, result={"rows_deleted": n})


def handle_cache_purge_expired(req: CachePurgeExpiredRequest) -> Reply:
    if _skip_during_vacuum():
        return Reply(rpc_id=req.rpc_id, ok=False, error="vacuum")
    writer = get_active_writer()
    try:
        n = cache.purge_expired_via_writer(writer).result(timeout=5)
    except TimeoutError:
        logger.warning("cache_purge_expired timeout: writer busy")
        return Reply(rpc_id=req.rpc_id, ok=False, error="writer busy")
    return Reply(rpc_id=req.rpc_id, ok=True, result={"rows_deleted": n})


def handle_ping(req: PingRequest) -> Reply:
    return Reply(rpc_id=req.rpc_id, ok=True)
