"""Module-level active-broker registration for Phase 2 of the
two-pool restructure (_claude/specs/2026-05-29-broker-two-pool-design.md).

The broker server registers its ReadSessionPool + WriterThread
here at startup so handler functions can reach them without
refactoring the dispatcher's per-RPC call shape. Single active
broker at any time, matching production (one broker process per
container). The internal lock guards register/clear so the
read-mostly accessors stay coherent across threads.

Phase 2 callers: mimir/broker/handlers/warm.py. Subsequent
phases (4, 5) add the cache and admin handlers."""

from __future__ import annotations

import threading
from typing import Optional

from mimir.broker.pools import ReadSessionPool
from mimir.broker.writes import WriterThread

# Lock guards every read and write of `_active_pool` /
# `_active_writer`. The read path is hot (every RPC dispatch hits
# `get_active_pool()` and `get_active_writer()` once each), and audit
# #483 proposed replacing the lock with a single immutable tuple read
# under the theory that free-threading serialises this otherwise.
# Microbenchmark `_claude/bench-483-context-lock.py` measured both
# shapes on GIL-on and GIL-off (3.14t) interpreters at N in {1, 2, 4,
# 8, 16} threads:
#
# - GIL on: lockless ~3x faster, scales linearly. Expected.
# - GIL off: lockless is 2.3x SLOWER at N=4, 1.6x slower at N=8 and
#   N=16. The lockless shape does two module-attribute loads + tuple
#   subscripts per call, and under free-threading each one trips
#   atomic refcount increments on a shared object. That refcount
#   thrashing dominates the lock acquire under contention; the lock
#   itself has built-in backoff semantics that scale better.
#
# At the broker's default N=8 warm workers the baseline costs ~131 ns
# per lookup × 2 lookups per RPC dispatch = ~260 ns of overhead per
# dispatch. Dispatch itself is ~10 ms, so the lookup is 0.003% of
# dispatch time. Not worth optimising; the proposed fix would
# regress. Lock stays.
_lock = threading.Lock()
_active_pool: Optional[ReadSessionPool] = None
_active_writer: Optional[WriterThread] = None


def set_active(pool: ReadSessionPool, writer: WriterThread) -> None:
    """Register the active broker's pool + writer. Replaces any
    previous registration; the caller owns the lifecycle of the
    old objects."""
    global _active_pool, _active_writer
    with _lock:
        _active_pool = pool
        _active_writer = writer


def clear_active() -> None:
    """Drop the registration. Idempotent."""
    global _active_pool, _active_writer
    with _lock:
        _active_pool = None
        _active_writer = None


def get_active_pool() -> ReadSessionPool:
    """Return the active broker's ReadSessionPool. Raises
    RuntimeError if no broker is active (e.g. tests that forgot
    to register, or a handler called outside the broker's
    lifecycle)."""
    with _lock:
        if _active_pool is None:
            raise RuntimeError("No active broker; call set_active() first")
        return _active_pool


def get_active_writer() -> WriterThread:
    """Return the active broker's WriterThread. Raises RuntimeError
    if no broker is active."""
    with _lock:
        if _active_writer is None:
            raise RuntimeError("No active broker; call set_active() first")
        return _active_writer
