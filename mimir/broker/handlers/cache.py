"""Cache RPC handlers: the one-way `cache_set` and `ping`.

`cache_set` is the only cache write that crosses the socket. Every cache
invalidation (`cache.delete`, `cache.delete_for_inbox`, the periodic
purge) runs inside the broker and submits to the writer directly, so no
reply ever has to wait on a cache commit.

During VACUUM, new cache writes are skipped before writer submission
and counted for one maintenance summary.
"""

import logging
import queue
import threading

from mimir import cache
from mimir.broker._context import get_active_writer
from mimir.broker.protocol import CacheSetRequest, PingRequest, Reply

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


def handle_cache_set(req: CacheSetRequest) -> None:
    """Queue the write and return nothing: the op is one-way, so the
    outcome goes to the log rather than to a caller.

    `req.key` is already namespaced and `req.value_json` already
    JSON-encoded (the web tier's `cache.set` did both, and checked the
    size cap, before sending), hence the nskey helper. A full writer
    queue drops the write rather than block the cache worker behind a
    held writer (ANALYZE): the next request for the page recomputes."""
    if _skip_during_vacuum():
        return
    try:
        future = cache._set_via_writer_for_nskey(
            get_active_writer(), req.key, req.value_json, req.ttl, block=False
        )
    except queue.Full:
        logger.warning("cache_set dropped: writer queue full (key=%s)", req.key)
        return

    def _log_failure(f):
        if (exc := f.exception()) is not None:
            logger.warning("cache_set failed (key=%s): %s", req.key, exc)

    future.add_done_callback(_log_failure)


def handle_ping(req: PingRequest) -> Reply:
    return Reply(rpc_id=req.rpc_id, ok=True)
