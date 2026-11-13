"""Cache-set admission, VACUUM admission state and ping.

Sets acknowledge writer admission without waiting for commit. During VACUUM,
reject new sets before submission and count them for one maintenance summary.
Internal broker invalidations use the active writer and wait for commit.
"""

import logging
import queue
import threading

from mimir import cache
from mimir.broker._context import get_active_writer
from mimir.broker.protocol import (
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


def is_vacuum_active() -> bool:
    """Let housekeeping defer without counting a skipped client mutation."""
    with _vacuum_lock:
        return bool(_active_vacuums)


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


def handle_ping(req: PingRequest) -> Reply:
    return Reply(rpc_id=req.rpc_id, ok=True)
