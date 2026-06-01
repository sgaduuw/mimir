"""WriteOp / WriteFuture / WriterThread tests, Phase 1 of the
two-pool restructure."""

import dataclasses
import threading
import time

from sqlalchemy import text

from mimir.broker.writes import WriteOp, WriterThread


def test_write_op_is_frozen():
    op = WriteOp(label="test", fn=lambda c: None)
    assert dataclasses.is_dataclass(op)
    # frozen=True so reassigning the label fails
    try:
        op.label = "other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("WriteOp should be frozen")


def test_write_op_holds_label_and_fn():
    sentinel = object()

    def fn(conn):
        return sentinel

    op = WriteOp(label="cache.set:k", fn=fn)
    assert op.label == "cache.set:k"
    assert op.fn is fn


def test_writer_thread_submit_commits_and_resolves_future(seeded_db):
    """submit() returns a Future, the op runs inside BEGIN IMMEDIATE,
    the COMMIT lands, and the future resolves with the closure's
    return value (in this case the SQLAlchemy CursorResult from execute)."""
    from mimir.config import settings

    writer = WriterThread(database_url=settings.database_url, queue_depth=8)
    writer.start()
    try:
        future = writer.submit(
            WriteOp(
                label="test:upsert",
                fn=lambda c: c.execute(
                    text(
                        "INSERT INTO cache (key, value, expires_at) "
                        "VALUES ('writer-thread-test-1', '{}', 9999999999)"
                    )
                ),
            )
        )
        result = future.result(timeout=5)
        assert result is not None  # CursorResult from execute()

        # Verify the row actually landed by opening a fresh connection.
        from sqlalchemy import create_engine

        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            row = c.execute(
                text("SELECT key FROM cache WHERE key = 'writer-thread-test-1'")
            ).scalar_one_or_none()
            assert row == "writer-thread-test-1"
        engine.dispose()
    finally:
        writer.stop(timeout=5)


def test_writer_thread_submit_returns_future_immediately(seeded_db):
    """submit() returns BEFORE the writer commits (asynchronous
    semantics). We block a slow op via a barrier and confirm
    submit() of a fast op behind it returns promptly."""
    from mimir.config import settings

    writer = WriterThread(database_url=settings.database_url, queue_depth=8)
    writer.start()
    try:
        gate = threading.Event()

        def slow_fn(c):
            gate.wait(timeout=5)
            c.execute(
                text(
                    "INSERT INTO cache (key, value, expires_at) "
                    "VALUES ('slow', '{}', 9999999999)"
                )
            )

        slow_future = writer.submit(WriteOp(label="test:slow", fn=slow_fn))
        # While slow_fn is blocked inside the writer thread, another
        # submit should still return immediately (it just enqueues).
        t0 = time.perf_counter()
        fast_future = writer.submit(
            WriteOp(
                label="test:fast",
                fn=lambda c: c.execute(
                    text(
                        "INSERT INTO cache (key, value, expires_at) "
                        "VALUES ('fast', '{}', 9999999999)"
                    )
                ),
            )
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 100, f"submit() blocked for {elapsed_ms}ms"
        gate.set()
        slow_future.result(timeout=5)
        fast_future.result(timeout=5)
    finally:
        writer.stop(timeout=5)


def test_writer_thread_stop_is_idempotent(seeded_db):
    from mimir.config import settings

    writer = WriterThread(database_url=settings.database_url, queue_depth=8)
    writer.start()
    writer.stop(timeout=5)
    writer.stop(timeout=5)  # second call is a no-op, not an error


def test_writer_thread_op_exception_sets_future_and_survives(seeded_db):
    """An op that raises does NOT kill the writer. The future
    receives the exception. A subsequent submit still commits."""
    from mimir.config import settings

    writer = WriterThread(database_url=settings.database_url, queue_depth=8)
    writer.start()
    try:

        def bad_fn(c):
            raise ValueError("boom")

        bad_future = writer.submit(WriteOp(label="test:bad", fn=bad_fn))
        with __import__("pytest").raises(ValueError, match="boom"):
            bad_future.result(timeout=5)

        # Writer should still be alive and accept the next op.
        good_future = writer.submit(
            WriteOp(
                label="test:good-after-bad",
                fn=lambda c: c.execute(
                    text(
                        "INSERT INTO cache (key, value, expires_at) "
                        "VALUES ('after-bad', '{}', 9999999999)"
                    )
                ),
            )
        )
        result = good_future.result(timeout=5)
        assert result is not None  # CursorResult from execute()
    finally:
        writer.stop(timeout=5)


def test_writer_thread_op_exception_rollbacks_transaction(seeded_db):
    """If fn writes some rows then raises, none of the writes commit.
    Verifies atomicity of the per-op transaction."""
    from mimir.config import settings

    writer = WriterThread(database_url=settings.database_url, queue_depth=8)
    writer.start()
    try:

        def partial_then_bad(c):
            c.execute(
                text(
                    "INSERT INTO cache (key, value, expires_at) "
                    "VALUES ('partial', '{}', 9999999999)"
                )
            )
            raise RuntimeError("rollback me")

        future = writer.submit(WriteOp(label="test:partial", fn=partial_then_bad))
        with __import__("pytest").raises(RuntimeError, match="rollback me"):
            future.result(timeout=5)

        # 'partial' should NOT be in the cache table.
        from sqlalchemy import create_engine

        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            row = c.execute(
                text("SELECT key FROM cache WHERE key = 'partial'")
            ).scalar_one_or_none()
            assert row is None
        engine.dispose()
    finally:
        writer.stop(timeout=5)


def test_writer_thread_submit_blocks_when_queue_full(seeded_db):
    """With queue_depth=2 and the writer blocked on a slow op, the
    next two submits fill the queue, and the THIRD submit blocks
    until the slow op completes. This is the backpressure contract."""
    from mimir.config import settings

    writer = WriterThread(database_url=settings.database_url, queue_depth=2)
    writer.start()
    try:
        gate = threading.Event()

        def slow(c):
            gate.wait(timeout=5)

        # Op 1 enters the writer thread immediately (taken off the queue).
        f1 = writer.submit(WriteOp(label="test:slow1", fn=slow))
        # Ops 2 and 3 sit in the queue (depth=2).
        f2 = writer.submit(WriteOp(label="test:queued1", fn=lambda c: None))
        f3 = writer.submit(WriteOp(label="test:queued2", fn=lambda c: None))

        # The 4th submit should BLOCK because the queue is full.
        blocked = [False]

        def attempt_blocked_submit():
            f4 = writer.submit(WriteOp(label="test:should-block", fn=lambda c: None))
            blocked[0] = True
            return f4

        t = threading.Thread(target=attempt_blocked_submit)
        t.start()
        time.sleep(0.2)
        assert not blocked[0], "submit() did not block on full queue"

        # Unblock the slow op; queue drains; 4th submit completes.
        gate.set()
        t.join(timeout=5)
        assert blocked[0], "submit() never unblocked"
        for f in (f1, f2, f3):
            f.result(timeout=5)
    finally:
        writer.stop(timeout=5)


def test_writer_thread_slow_commit_emits_warning(seeded_db, caplog):
    """A commit that exceeds broker_slow_rpc_warn_ms emits a WARNING
    line with the op label and elapsed ms."""
    from mimir.config import settings

    writer = WriterThread(
        database_url=settings.database_url,
        queue_depth=8,
        slow_warn_ms=10,  # very low threshold so the test reliably triggers
    )
    writer.start()
    try:

        def slow(c):
            time.sleep(0.05)  # 50ms > 10ms threshold

        with caplog.at_level("WARNING", logger="mimir.broker.writes"):
            f = writer.submit(WriteOp(label="test:slowcommit", fn=slow))
            f.result(timeout=5)

        slow_lines = [r for r in caplog.records if "slow write" in r.message]
        assert slow_lines, (
            f"expected slow-write warning, got: {[r.message for r in caplog.records]}"
        )
        msg = slow_lines[0].message
        assert "test:slowcommit" in msg
        # Format check: "broker slow write [<label>] (<ms>ms)"
        assert "ms" in msg
    finally:
        writer.stop(timeout=5)


def test_writer_thread_fast_commit_no_warning(seeded_db, caplog):
    from mimir.config import settings

    writer = WriterThread(
        database_url=settings.database_url,
        queue_depth=8,
        slow_warn_ms=5000,  # high threshold so a fast op stays under it
    )
    writer.start()
    try:
        with caplog.at_level("WARNING", logger="mimir.broker.writes"):
            f = writer.submit(WriteOp(label="test:fastcommit", fn=lambda c: None))
            f.result(timeout=5)
        assert not [r for r in caplog.records if "slow write" in r.message]
    finally:
        writer.stop(timeout=5)


def test_write_future_propagates_closure_return_value(seeded_db):
    """A WriteOp whose closure returns a value should expose that
    value via WriteFuture.result(). Mirror of concurrent.futures.Future
    semantics. Closures that return None implicitly are unaffected.

    Phase 4 of the broker two-pool restructure relies on this so the
    cache delete-family handlers can return rowcount in their RPC
    reply without a captured-variable workaround.
    """
    from mimir.broker.writes import WriteOp, WriterThread

    wt = WriterThread.from_settings()
    wt.start()
    try:

        def _returns_value(conn):
            return 42

        fut = wt.submit(WriteOp(label="test:propagate", fn=_returns_value))
        assert fut.result(timeout=10) == 42

        def _returns_none(conn):
            return None

        fut2 = wt.submit(WriteOp(label="test:none", fn=_returns_none))
        assert fut2.result(timeout=10) is None

        def _no_return(conn):
            pass  # implicit None return

        fut3 = wt.submit(WriteOp(label="test:noreturn", fn=_no_return))
        assert fut3.result(timeout=10) is None
    finally:
        wt.stop(timeout=10)


def test_writer_thread_engine_has_sqlite_pragmas_set(seeded_db):
    """The WriterThread creates its own engine via create_engine();
    until this test was added, that engine had no event listeners
    attached, so PRAGMA foreign_keys was OFF (silently breaking
    ondelete=CASCADE), PRAGMA synchronous defaulted to FULL (extra
    fsync per commit), PRAGMA analysis_limit was 0 (ANALYZE held
    the writer lock for the full multi-second scan on the prod
    corpus), and PRAGMA busy_timeout was 0.

    The fix attaches `mimir.extensions._sqlite_pragmas` as a connect
    listener on the writer's engine. This test pins the contract by
    submitting a closure that reads back the PRAGMA values from the
    writer's connection.

    Notable exclusion: PRAGMA query_only must stay OFF on the writer
    (the writer IS the writer). The `_sqlite_pragmas` listener
    correctly skips `query_only=1` when `settings.mimir_is_broker`
    is true; the broker process sets that env var. Tests run without
    `MIMIR_IS_BROKER=true`, so the listener WOULD apply query_only=1
    to a non-broker shared-engine connection, but the writer's
    engine here is process-local and we're testing the listener's
    "broker == sole writer" semantics. The query_only check is
    omitted from this test for that reason; if it failed (i.e. the
    listener wrongly set query_only on the writer in a real broker
    deploy) the integration tests would surface the
    `OperationalError: attempt to write a readonly database`.
    """
    from mimir.broker.writes import WriteOp, WriterThread
    from mimir.config import settings

    writer = WriterThread(database_url=settings.database_url, queue_depth=8)
    writer.start()
    try:

        def _read_pragmas(conn):
            return {
                "foreign_keys": conn.exec_driver_sql("PRAGMA foreign_keys").scalar(),
                "synchronous": conn.exec_driver_sql("PRAGMA synchronous").scalar(),
                "busy_timeout": conn.exec_driver_sql("PRAGMA busy_timeout").scalar(),
                "analysis_limit": conn.exec_driver_sql(
                    "PRAGMA analysis_limit"
                ).scalar(),
            }

        result = writer.submit(
            WriteOp(label="test:read-pragmas", fn=_read_pragmas)
        ).result(timeout=5)

        assert result["foreign_keys"] == 1, (
            "PRAGMA foreign_keys must be ON; without it, DELETE statements "
            "that rely on ondelete=CASCADE silently leave child rows behind"
        )
        assert result["synchronous"] == 1, (
            "PRAGMA synchronous must be NORMAL (=1); FULL (=2) is the SQLite "
            "default but adds a needless fsync per commit on WAL mode"
        )
        assert result["busy_timeout"] == settings.sqlite_busy_timeout_ms, (
            f"PRAGMA busy_timeout must match settings.sqlite_busy_timeout_ms "
            f"({settings.sqlite_busy_timeout_ms}); got {result['busy_timeout']}"
        )
        assert result["analysis_limit"] == settings.analyze_limit, (
            f"PRAGMA analysis_limit must match settings.analyze_limit "
            f"({settings.analyze_limit}); got {result['analysis_limit']}. "
            "0 would mean unbounded ANALYZE scans, which held the writer "
            "lock for ~25s on the 11M-row prod corpus before 1.36.4"
        )
    finally:
        writer.stop(timeout=5)
