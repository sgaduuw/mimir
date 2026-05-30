"""Free-threading-specific race tests for Phase 1 of the broker
two-pool restructure. Runs under both Python 3.14 (GIL on) and
3.14t (free-threaded) via the existing CI matrix (the test +
test-ft jobs added in 2.9.0).

These tests use threading.Barrier to force contention. Each one
loops N times to surface intermittent races; if a race exists,
running under 3.14t will eventually expose it."""

import threading
from concurrent.futures import wait

import pytest
from sqlalchemy import create_engine, text

from mimir.broker.pools import ReadSessionPool
from mimir.broker.writes import WriteOp, WriterThread
from mimir.config import settings


@pytest.mark.parametrize("iterations", [20])
def test_two_threads_cache_set_same_key_last_wins(seeded_db, iterations):
    """Two threads cache.set-style WriteOps on the same key. Last
    committed value wins (no partial-row corruption, no
    interleaving). Run in a loop to surface intermittent races
    under free-threading."""
    writer = WriterThread.from_settings()
    writer.start()
    try:
        for i in range(iterations):
            barrier = threading.Barrier(2)
            results = []

            def worker(tag: str):
                barrier.wait(timeout=5)
                f = writer.submit(
                    WriteOp(
                        label=f"test:race:{tag}",
                        fn=lambda c, t=tag: c.execute(
                            text(
                                "INSERT INTO cache (key, value, expires_at) "
                                "VALUES ('race-key', :v, 9999999999) "
                                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                            ),
                            {"v": f'"{t}"'},
                        ),
                    )
                )
                results.append(f)

            t_a = threading.Thread(target=worker, args=("A",))
            t_b = threading.Thread(target=worker, args=("B",))
            t_a.start()
            t_b.start()
            t_a.join(timeout=5)
            t_b.join(timeout=5)
            wait(results, timeout=5)

            engine = create_engine(settings.database_url, future=True)
            with engine.begin() as c:
                row = c.execute(
                    text("SELECT value FROM cache WHERE key = 'race-key'")
                ).scalar_one()
                assert row in ('"A"', '"B"'), f"iter {i}: got {row!r}"
                c.execute(text("DELETE FROM cache WHERE key = 'race-key'"))
            engine.dispose()
    finally:
        writer.stop(timeout=5)


def test_writer_fifo_ordering_under_concurrent_submit(seeded_db):
    """N threads each submit one op simultaneously. The writer
    serialises them in some order; what we pin is: every op
    commits, no future is left unresolved, no exception leaks."""
    writer = WriterThread.from_settings()
    writer.start()
    try:
        N = 16
        barrier = threading.Barrier(N)
        futures = []
        lock = threading.Lock()

        def worker(idx: int):
            barrier.wait(timeout=5)
            f = writer.submit(
                WriteOp(
                    label=f"test:fifo:{idx}",
                    fn=lambda c, i=idx: c.execute(
                        text(
                            "INSERT INTO cache (key, value, expires_at) "
                            "VALUES (:k, '{}', 9999999999)"
                        ),
                        {"k": f"fifo-{i}"},
                    ),
                )
            )
            with lock:
                futures.append(f)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        wait(futures, timeout=10)
        for f in futures:
            assert f.exception() is None
            assert f.result() is None

        engine = create_engine(settings.database_url, future=True)
        with engine.begin() as c:
            count = c.execute(
                text("SELECT COUNT(*) FROM cache WHERE key LIKE 'fifo-%'")
            ).scalar_one()
            assert count == N
            c.execute(text("DELETE FROM cache WHERE key LIKE 'fifo-%'"))
        engine.dispose()
    finally:
        writer.stop(timeout=10)


def test_read_pool_concurrent_pragma_query_only_holds(seeded_db):
    """N threads each open a session and verify PRAGMA query_only
    really is 1. Under free-threading, if the connect-event listener
    has any racy initialisation, multiple threads might see
    query_only=0. Loop to surface."""
    pool = ReadSessionPool.from_settings()
    try:
        N = 8
        iters_per_thread = 5
        barrier = threading.Barrier(N)
        errors: list[str] = []

        def worker():
            try:
                barrier.wait(timeout=5)
                for _ in range(iters_per_thread):
                    with pool.session() as s:
                        v = s.execute(text("PRAGMA query_only")).scalar_one()
                        if v != 1:
                            errors.append(f"saw query_only={v}")
            except Exception as e:
                errors.append(repr(e))

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"race detected: {errors}"
    finally:
        pool.close()


def test_thread_local_context_var_isolation_under_free_threading():
    """Pin the stdlib ContextVar per-thread isolation contract.
    Today's `mimir.cache.refresh_window` ContextVar was implicitly
    safe under the GIL; under free-threading we verify that thread
    isolation still holds in the stdlib so the cache module's
    contract continues to work."""
    from contextvars import ContextVar

    cv: ContextVar[int] = ContextVar("test_cv", default=-1)
    seen: dict[int, int] = {}

    def worker(tid: int, value: int):
        cv.set(value)
        seen[tid] = cv.get()

    threads = [threading.Thread(target=worker, args=(i, i * 100)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    for tid in range(4):
        assert seen[tid] == tid * 100, f"thread {tid} saw {seen[tid]!r}"
