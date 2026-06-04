"""Cache-op handlers. Each is a sub-ms operation: dispatch a
WriteOp through the active WriterThread, await `.result()` for
the rowcount, return the reply.

Phase 4 of the broker two-pool restructure
(`_claude/specs/2026-05-29-broker-two-pool-design.md`): the
handlers no longer run `_direct_*(...)` inline on the cache
worker thread. Instead, they submit single-statement WriteOps
through `_context.get_active_writer()` and await `.result()`.
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
The web tier's `cache.set` is already best-effort by contract
(it logs + swallows non-OK replies; the cached value still ages
out via TTL). Without this catch, a long writer-lock hold
(VACUUM in particular, ~minutes per cycle) trips
`TimeoutError` per cache RPC and `dispatch`'s outer exception
handler logs each one as an ERROR with full traceback, which
is noisy. The same outcome (Reply ok=False, swallowed by
client) is the documented "VACUUM holds the writer lock; cache
writes fail during the window" posture from CONTEXT.md.
"""

import logging

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


def handle_cache_set(req: CacheSetRequest) -> Reply:
    writer = get_active_writer()
    # req.key is already namespaced and req.value_json already JSON-encoded
    # (the web tier's cache.set did both before sending). _set_via_writer_for_nskey
    # parallels _direct_set's pre-prepared-inputs signature. Await .result()
    # so the reply doesn't go out before the commit lands.
    try:
        cache._set_via_writer_for_nskey(
            writer, req.key, req.value_json, req.ttl
        ).result(timeout=5)
    except TimeoutError:
        logger.warning("cache_set timeout: writer busy (key=%s)", req.key)
        return Reply(rpc_id=req.rpc_id, ok=False, error="writer busy")
    return Reply(rpc_id=req.rpc_id, ok=True)


def handle_cache_delete(req: CacheDeleteRequest) -> Reply:
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
    writer = get_active_writer()
    try:
        n = cache.delete_for_inbox_via_writer(writer, req.name).result(timeout=5)
    except TimeoutError:
        logger.warning(
            "cache_delete_for_inbox timeout: writer busy (name=%s)", req.name
        )
        return Reply(rpc_id=req.rpc_id, ok=False, error="writer busy")
    return Reply(rpc_id=req.rpc_id, ok=True, rows_deleted=n)


def handle_cache_purge_expired(req: CachePurgeExpiredRequest) -> Reply:
    writer = get_active_writer()
    try:
        n = cache.purge_expired_via_writer(writer).result(timeout=5)
    except TimeoutError:
        logger.warning("cache_purge_expired timeout: writer busy")
        return Reply(rpc_id=req.rpc_id, ok=False, error="writer busy")
    return Reply(rpc_id=req.rpc_id, ok=True, rows_deleted=n)


def handle_ping(req: PingRequest) -> Reply:
    return Reply(rpc_id=req.rpc_id, ok=True)
