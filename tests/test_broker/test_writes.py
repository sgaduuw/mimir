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
    the COMMIT lands, and the future resolves with None."""
    from mimir.config import settings

    writer = WriterThread(database_url=settings.database_url, queue_depth=8)
    writer.start()
    try:
        future = writer.submit(
            WriteOp(
                label="test:upsert",
                fn=lambda c: c.execute(
                    text(
                        "INSERT INTO cache (key, value_json, expires_at) "
                        "VALUES ('writer-thread-test-1', '{}', 9999999999)"
                    )
                ),
            )
        )
        result = future.result(timeout=5)
        assert result is None

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
                    "INSERT INTO cache (key, value_json, expires_at) "
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
                        "INSERT INTO cache (key, value_json, expires_at) "
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
