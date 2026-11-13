"""Cache RPC admission and commit replies.

The cache worker submits mutations in order. Invalidations wait for admission
only when the writer queue is full, so deletes cannot be lost or overtaken.
Sets acknowledge admission; deletes and purges return a pending
reply for the completion worker to resolve after commit, within five seconds.
During VACUUM, reject new mutations before writer submission and count them
for one maintenance summary. Internal broker invalidations still wait through
cache._delete_routed before warming or returning to their caller.
"""

import logging
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field

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


@dataclass
class PendingCacheReply:
    """An admitted DELETE, with its original commit deadline and wire shape."""

    future: Future[int]
    rpc_id: int
    op: str
    label: str
    include_count: bool = True
    deadline: float = field(default_factory=lambda: time.monotonic() + 5.0)
    # Delivery retries reuse the same commit outcome and must not repeat errors.
    resolved_reply: Reply | None = field(default=None, init=False)

    def busy(self, reason: str) -> Reply:
        logger.warning("%s: writer busy (%s)", self.label, reason)

        # Admission cannot be undone. If this RPC no longer observes the
        # commit, preserve failures in logs without socket I/O on the writer.
        def log_failure(future):
            if (exc := future.exception()) is not None and not isinstance(
                exc, TimeoutError
            ):
                logger.warning("%s failed after reply: %s", self.label, exc)

        self.future.add_done_callback(log_failure)
        return Reply(rpc_id=self.rpc_id, ok=False, error="writer busy")

    def resolve(self) -> Reply:
        try:
            count = self.future.result(
                timeout=max(0.0, self.deadline - time.monotonic())
            )
        except TimeoutError:
            return self.busy("commit timeout")
        return Reply(
            rpc_id=self.rpc_id,
            ok=True,
            result={"rows_deleted": count} if self.include_count else None,
        )


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


def handle_cache_delete(req: CacheDeleteRequest) -> Reply | PendingCacheReply:
    if _skip_during_vacuum():
        return Reply(rpc_id=req.rpc_id, ok=False, error="vacuum")
    writer = get_active_writer()
    try:
        future = cache.delete_via_writer(writer, req.key, block=False)
    except queue.Full:
        # Preserve invalidations and their order with later sets under saturation.
        future = cache.delete_via_writer(writer, req.key)
    return PendingCacheReply(
        future, req.rpc_id, req.op, f"cache_delete (key={req.key})", include_count=False
    )


def handle_cache_delete_for_inbox(
    req: CacheDeleteForInboxRequest,
) -> Reply | PendingCacheReply:
    if _skip_during_vacuum():
        return Reply(rpc_id=req.rpc_id, ok=False, error="vacuum")
    writer = get_active_writer()
    try:
        future = cache.delete_for_inbox_via_writer(writer, req.name, block=False)
    except queue.Full:
        future = cache.delete_for_inbox_via_writer(writer, req.name)
    return PendingCacheReply(
        future, req.rpc_id, req.op, f"cache_delete_for_inbox (name={req.name})"
    )


def handle_cache_purge_expired(
    req: CachePurgeExpiredRequest,
) -> Reply | PendingCacheReply:
    if _skip_during_vacuum():
        return Reply(rpc_id=req.rpc_id, ok=False, error="vacuum")
    writer = get_active_writer()
    try:
        future = cache.purge_expired_via_writer(writer, block=False)
    except queue.Full:
        future = cache.purge_expired_via_writer(writer)
    return PendingCacheReply(future, req.rpc_id, req.op, "cache_purge_expired")


def handle_ping(req: PingRequest) -> Reply:
    return Reply(rpc_id=req.rpc_id, ok=True)
