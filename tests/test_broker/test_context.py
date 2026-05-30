"""Tests for the broker's module-level active-pool/writer registration.

Phase 2 of the two-pool restructure uses this so the warm handler
functions can reach the broker's ReadSessionPool + WriterThread
without a dispatcher refactor. Server lifecycle owns the registration."""

import pytest

from mimir.broker import _context
from mimir.broker.pools import ReadSessionPool
from mimir.broker.writes import WriterThread


@pytest.fixture
def active_pair(seeded_db):
    pool = ReadSessionPool.from_settings()
    writer = WriterThread.from_settings()
    writer.start()
    try:
        yield pool, writer
    finally:
        writer.stop(timeout=5)
        pool.close()
        _context.clear_active()


def test_context_get_active_raises_when_unset():
    _context.clear_active()
    with pytest.raises(RuntimeError, match="No active broker"):
        _context.get_active_pool()
    with pytest.raises(RuntimeError, match="No active broker"):
        _context.get_active_writer()


def test_context_set_active_then_get_returns_same_objects(active_pair):
    pool, writer = active_pair
    _context.set_active(pool, writer)
    assert _context.get_active_pool() is pool
    assert _context.get_active_writer() is writer


def test_context_clear_active_resets(active_pair):
    pool, writer = active_pair
    _context.set_active(pool, writer)
    _context.clear_active()
    with pytest.raises(RuntimeError):
        _context.get_active_pool()
    with pytest.raises(RuntimeError):
        _context.get_active_writer()


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
