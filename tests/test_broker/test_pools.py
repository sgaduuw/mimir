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


def test_read_session_pool_session_close_race(seeded_db):
    """TOCTOU regression test: concurrent close() while another
    thread is mid-session() must not produce a session bound to a
    disposed engine. The session() call either succeeds (and we use
    the session normally before close drains everything) or raises
    `RuntimeError("closed")`. It MUST NOT return a session that then
    throws an obscure SQLAlchemy error on first use."""
    import threading

    pool = ReadSessionPool.from_settings()
    errors: list[str] = []

    barrier = threading.Barrier(2)

    def opener():
        try:
            barrier.wait(timeout=5)
            with pool.session() as s:
                # If we got a session, it must be usable.
                row = s.execute(text("SELECT 1")).scalar_one()
                assert row == 1
        except RuntimeError as e:
            if "closed" not in str(e):
                errors.append(f"opener got unexpected RuntimeError: {e}")
        except Exception as e:
            errors.append(
                f"opener got non-RuntimeError exception: {type(e).__name__}: {e}"
            )

    def closer():
        try:
            barrier.wait(timeout=5)
            pool.close()
        except Exception as e:
            errors.append(f"closer raised: {e}")

    t_open = threading.Thread(target=opener)
    t_close = threading.Thread(target=closer)
    t_open.start()
    t_close.start()
    t_open.join(timeout=5)
    t_close.join(timeout=5)

    assert not errors, f"race produced bad state: {errors}"


def test_read_session_pool_close_waits_for_in_flight_session(seeded_db):
    """`close()` blocks while a session is checked out and only proceeds
    to `engine.dispose()` once the in-flight count drops to zero.
    Pre-fix `close()` would dispose mid-query, invalidating the live
    session's connection. Audit #472."""
    pool = ReadSessionPool.from_settings()

    session_acquired = threading.Event()
    release_session = threading.Event()
    close_returned = threading.Event()
    errors: list[str] = []

    def hold_session():
        try:
            with pool.session() as s:
                session_acquired.set()
                # Hold the session past close()'s arrival.
                if not release_session.wait(timeout=5.0):
                    errors.append("release_session never set")
                    return
                # Session must still be usable after close() has been
                # called and is blocked waiting.
                row = s.execute(text("SELECT 1")).scalar_one()
                if row != 1:
                    errors.append(f"session unusable mid-close: row={row!r}")
        except Exception as e:
            errors.append(f"hold_session raised: {type(e).__name__}: {e}")

    def trigger_close():
        try:
            pool.close(drain_timeout=5.0)
        except Exception as e:
            errors.append(f"close raised: {type(e).__name__}: {e}")
        finally:
            close_returned.set()

    t_hold = threading.Thread(target=hold_session)
    t_hold.start()
    assert session_acquired.wait(timeout=5.0), "session never acquired"

    t_close = threading.Thread(target=trigger_close)
    t_close.start()

    # Give close() a beat to enter wait_for; it should be blocked
    # because the session is still in-flight.
    time.sleep(0.25)
    assert not close_returned.is_set(), (
        "close() returned before the in-flight session was released; "
        "the drain barrier is not waiting"
    )

    # Release the session — close() should now complete.
    release_session.set()
    t_hold.join(timeout=5.0)
    t_close.join(timeout=10.0)

    assert close_returned.is_set(), "close() did not return after session released"
    assert not errors, errors


def test_read_session_pool_close_drain_timeout_proceeds_anyway(seeded_db, caplog):
    """If a session holds past `drain_timeout`, `close()` logs a WARNING
    and proceeds to `engine.dispose()` rather than blocking shutdown
    forever. Pre-fix behaviour: stuck handler hits OperationalError
    on its next statement, same as before, but the broker shuts down."""
    import logging

    pool = ReadSessionPool.from_settings()
    session_acquired = threading.Event()
    release_session = threading.Event()
    errors: list[str] = []

    def hold_session():
        try:
            with pool.session() as _s:
                session_acquired.set()
                release_session.wait(timeout=10.0)
        except Exception as e:
            errors.append(f"hold_session raised: {type(e).__name__}: {e}")

    t_hold = threading.Thread(target=hold_session)
    t_hold.start()
    assert session_acquired.wait(timeout=5.0)

    with caplog.at_level(logging.WARNING, logger="mimir.broker.pools"):
        pool.close(drain_timeout=0.3)
    release_session.set()
    t_hold.join(timeout=5.0)

    assert any(
        "still in-flight" in r.message and "disposing engine anyway" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]
    assert not errors, errors
