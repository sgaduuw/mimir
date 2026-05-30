"""Tests for the broker's module-level active-pool/writer registration.

Phase 2 of the two-pool restructure uses this so the warm handler
functions can reach the broker's ReadSessionPool + WriterThread
without a dispatcher refactor. Server lifecycle owns the registration.

Every test that mutates _context module state saves and restores the
previous pool + writer in a finally block. The session-scoped
_session_broker fixture in conftest.py registers a pool + writer for
the whole session; tests that call clear_active() or set_active() must
not leave the global in a state that breaks subsequent tests."""

import pytest

from mimir.broker import _context
from mimir.broker.pools import ReadSessionPool
from mimir.broker.writes import WriterThread


@pytest.fixture
def active_pair(seeded_db):
    """Yield (pool, writer) and restore the session-wide active context
    on teardown. The fixture does NOT register the pair as active;
    each test that needs them active calls _context.set_active() itself,
    so the test owns the registration lifecycle."""
    saved_pool = _context._active_pool
    saved_writer = _context._active_writer
    pool = ReadSessionPool.from_settings()
    writer = WriterThread.from_settings()
    writer.start()
    try:
        yield pool, writer
    finally:
        writer.stop(timeout=5)
        pool.close()
        # Restore the session broker's registration so subsequent
        # tests can still reach the active pool.
        if saved_pool is not None and saved_writer is not None:
            _context.set_active(saved_pool, saved_writer)
        else:
            _context.clear_active()


def test_context_get_active_raises_when_unset():
    saved_pool = _context._active_pool
    saved_writer = _context._active_writer
    _context.clear_active()
    try:
        with pytest.raises(RuntimeError, match="No active broker"):
            _context.get_active_pool()
        with pytest.raises(RuntimeError, match="No active broker"):
            _context.get_active_writer()
    finally:
        if saved_pool is not None and saved_writer is not None:
            _context.set_active(saved_pool, saved_writer)


def test_context_set_active_then_get_returns_same_objects(active_pair):
    pool, writer = active_pair
    _context.set_active(pool, writer)
    assert _context.get_active_pool() is pool
    assert _context.get_active_writer() is writer
    # active_pair's teardown restores the session context.


def test_context_clear_active_resets(active_pair):
    pool, writer = active_pair
    _context.set_active(pool, writer)
    _context.clear_active()
    with pytest.raises(RuntimeError):
        _context.get_active_pool()
    with pytest.raises(RuntimeError):
        _context.get_active_writer()
    # active_pair's teardown restores the session context.


def test_context_set_active_replaces_previous(active_pair, seeded_db):
    pool1, writer1 = active_pair
    _context.set_active(pool1, writer1)

    pool2 = ReadSessionPool.from_settings()
    writer2 = WriterThread.from_settings()
    writer2.start()
    try:
        _context.set_active(pool2, writer2)
        assert _context.get_active_pool() is pool2
        assert _context.get_active_writer() is writer2
    finally:
        writer2.stop(timeout=5)
        pool2.close()
    # active_pair's teardown restores the session context.
