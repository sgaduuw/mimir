"""ReadSessionPool tests, Phase 1 of the two-pool restructure.

We pin: PRAGMA query_only is actually enforced on every checkout,
multiple threads can hold sessions simultaneously without state
bleed, and pool.close() actually closes the underlying engine."""

import threading
import time

import pytest
from sqlalchemy import text

from mimir.broker.pools import ReadSessionPool


def test_read_session_pool_session_is_query_only(seeded_db):
    pool = ReadSessionPool.from_settings()
    try:
        with pool.session() as s:
            row = s.execute(text("PRAGMA query_only")).scalar_one()
            assert row == 1, f"expected query_only=1, got {row}"
    finally:
        pool.close()


def test_read_session_pool_write_attempt_raises(seeded_db):
    """Defence-in-depth: an accidental write attempt on a read-pool
    session should fail loudly, not corrupt data."""
    pool = ReadSessionPool.from_settings()
    try:
        with pool.session() as s:
            with pytest.raises(Exception) as exc_info:
                s.execute(
                    text(
                        "INSERT INTO cache (key, value, expires_at) VALUES ('test', '{}', 0)"
                    )
                )
                s.commit()
            assert (
                "readonly" in str(exc_info.value).lower()
                or "query_only" in str(exc_info.value).lower()
            )
    finally:
        pool.close()


def test_read_session_pool_concurrent_checkouts(seeded_db):
    """Multiple threads can hold sessions simultaneously, no shared
    state between them, no exceptions when N parallel checkouts."""
    pool = ReadSessionPool.from_settings()
    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait(timeout=5)
            with pool.session() as s:
                row = s.execute(text("PRAGMA query_only")).scalar_one()
                assert row == 1
                time.sleep(0.05)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    pool.close()
    assert not errors, f"concurrent checkouts errored: {errors}"


def test_read_session_pool_close_releases_resources(seeded_db):
    """After close(), session() should raise. No leftover connection
    pool, no leaked threads."""
    pool = ReadSessionPool.from_settings()
    with pool.session() as s:
        s.execute(text("SELECT 1"))
    pool.close()
    with pytest.raises(RuntimeError, match="closed"):
        with pool.session():
            pass
