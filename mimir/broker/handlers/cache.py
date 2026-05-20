"""Cache-op handlers. Each is a sub-ms operation: open a session,
run one upsert / delete / select, commit. These route to the
broker's **cache worker** (the cache_queue) because they need the
fast lane even when the long worker is mid-commit on a slow op.

`ping` lives here too: it doesn't touch the DB at all (just a
liveness probe used by the compose healthcheck) but classifies as
a cache-family op for routing purposes since it's expected to be
sub-ms.

Imports the `_direct_*` helpers from `mimir.cache` rather than the
public functions so the broker doesn't recurse into broker-mode
dispatch and infinite-loop.
"""
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


def handle_cache_set(req: CacheSetRequest) -> Reply:
    with write_transaction("broker:cache_set"):
        cache._direct_set(req.key, req.value_json, req.ttl)
    return Reply(ok=True)


def handle_cache_delete(req: CacheDeleteRequest) -> Reply:
    with write_transaction("broker:cache_delete"):
        cache._direct_delete(req.key)
    return Reply(ok=True)


def handle_cache_delete_for_inbox(req: CacheDeleteForInboxRequest) -> Reply:
    with write_transaction("broker:cache_delete_for_inbox"):
        n = cache._direct_delete_for_inbox(req.name)
    return Reply(ok=True, rows_deleted=n)


def handle_cache_purge_expired(req: CachePurgeExpiredRequest) -> Reply:
    with write_transaction("broker:cache_purge_expired"):
        n = cache._direct_purge_expired()
    return Reply(ok=True, rows_deleted=n)


def handle_ping(req: PingRequest) -> Reply:
    return Reply(ok=True)
