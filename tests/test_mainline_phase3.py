"""Phase 3 tests for mimir.mainline batched writer dispatch.

These pin the new batch shape introduced by the broker two-pool
restructure (`_claude/specs/2026-05-29-broker-two-pool-design.md`).
Run alongside the existing test_mainline tests; kept in a separate
file so the Phase 3 PR audit is easy."""

from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from mimir.broker.writes import WriterThread
from mimir.config import settings
from mimir.mainline import _submit_mainline_batch


def _row(commit_sha: str, message_id: str, tree_name: str = "linus"):
    return {
        "commit_sha": commit_sha,
        "message_id": message_id,
        "tree_name": tree_name,
        "committed_at": datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc),
    }


def test_submit_mainline_batch_inserts_rows_via_writer(seeded_db):
    """_submit_mainline_batch composes a WriteOp wrapping the
    INSERT OR IGNORE for a batch of (commit_sha, message_id) pairs,
    submits it to the writer, and the rows appear after the writer
    drains."""
    writer = WriterThread.from_settings()
    writer.start()
    try:
        batch = [
            _row("a" * 40, "msg-a@kernel.org"),
            _row("b" * 40, "msg-b@kernel.org"),
        ]
        future = _submit_mainline_batch(writer, "linus", batch)
        future.result(timeout=5)

        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            count = c.execute(
                text(
                    "SELECT COUNT(*) FROM mainline_commits "
                    "WHERE tree_name = 'linus' AND commit_sha IN (:a, :b)"
                ),
                {"a": "a" * 40, "b": "b" * 40},
            ).scalar_one()
            assert count == 2
            # Commit the implicit read transaction before starting the
            # cleanup DELETE; SQLAlchemy 2.0 autobegin means the SELECT
            # above already opened a transaction on this connection.
            c.commit()
            c.execute(
                text(
                    "DELETE FROM mainline_commits "
                    "WHERE tree_name = 'linus' AND commit_sha IN (:a, :b)"
                ),
                {"a": "a" * 40, "b": "b" * 40},
            )
            c.commit()
        engine.dispose()
    finally:
        writer.stop(timeout=5)


def test_submit_mainline_batch_is_idempotent_on_replay(seeded_db):
    """If a batch is submitted twice (e.g. the next tick re-walks
    after a crash), the on_conflict_do_nothing makes the second
    submission a no-op rather than a constraint error.
    Verified by re-submitting the same batch and asserting row
    count stays at the original size."""
    writer = WriterThread.from_settings()
    writer.start()
    try:
        batch = [
            _row("c" * 40, "msg-c@kernel.org"),
        ]
        _submit_mainline_batch(writer, "linus", batch).result(timeout=5)
        # Second submission -- should not raise; should be a no-op.
        _submit_mainline_batch(writer, "linus", batch).result(timeout=5)

        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            count = c.execute(
                text(
                    "SELECT COUNT(*) FROM mainline_commits "
                    "WHERE tree_name = 'linus' AND commit_sha = :s"
                ),
                {"s": "c" * 40},
            ).scalar_one()
            assert count == 1
            # Commit the implicit read transaction before starting the
            # cleanup DELETE; same SQLAlchemy 2.0 autobegin reason as
            # the first test.
            c.commit()
            c.execute(
                text(
                    "DELETE FROM mainline_commits "
                    "WHERE tree_name = 'linus' AND commit_sha = :s"
                ),
                {"s": "c" * 40},
            )
            c.commit()
        engine.dispose()
    finally:
        writer.stop(timeout=5)


def test_submit_mainline_batch_empty_batch_is_noop(seeded_db):
    """Empty batch returns a pre-resolved future without submitting
    anything to the writer (so the writer doesn't get spurious
    no-op WriteOps that just waste a transaction)."""
    writer = WriterThread.from_settings()
    writer.start()
    try:
        future = _submit_mainline_batch(writer, "linus", [])
        # Future should resolve immediately; .result() with timeout=0
        # should work (would block if a real submit happened).
        assert future.done()
        assert future.result() is None
    finally:
        writer.stop(timeout=5)


def test_submit_mainline_cursor_update_advances_state_via_writer(seeded_db):
    """_submit_mainline_cursor_update composes a WriteOp wrapping the
    MainlineState upsert for a tree's commits_walked_to_sha cursor,
    submits it to the writer, and the state row reflects the new
    SHA after the writer drains.

    UPSERT semantics: works whether the row exists or not.
    """
    from sqlalchemy import create_engine, text

    from mimir.broker.writes import WriterThread
    from mimir.config import settings
    from mimir.mainline import _submit_mainline_cursor_update

    writer = WriterThread.from_settings()
    writer.start()
    try:
        # First update -- row may or may not exist for tree "linus"
        # from a prior test; either way the upsert should leave the
        # cursor at "d" * 40.
        future = _submit_mainline_cursor_update(writer, "linus", "d" * 40)
        future.result(timeout=5)

        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            sha = c.execute(
                text(
                    "SELECT commits_walked_to_sha FROM mainline_state "
                    "WHERE tree_name = 'linus'"
                )
            ).scalar_one_or_none()
            assert sha == "d" * 40
            c.commit()

            # Reset for other tests -- set cursor back to NULL.
            c.execute(
                text(
                    "UPDATE mainline_state SET commits_walked_to_sha = NULL "
                    "WHERE tree_name = 'linus'"
                )
            )
            c.commit()
        engine.dispose()
    finally:
        writer.stop(timeout=5)


def test_submit_mainline_cursor_update_creates_row_when_missing(seeded_db):
    """If MainlineState has no row for this tree yet, the upsert
    creates one with the cursor set. Covers the "new tree" boot
    case where MainlineState may be empty."""
    from sqlalchemy import create_engine, text

    from mimir.broker.writes import WriterThread
    from mimir.config import settings
    from mimir.mainline import _submit_mainline_cursor_update

    writer = WriterThread.from_settings()
    writer.start()
    engine = create_engine(settings.database_url, future=True)
    try:
        # Ensure no row exists for tree "phase3-new".
        with engine.connect() as c:
            c.execute(text("DELETE FROM mainline_state WHERE tree_name = 'phase3-new'"))
            c.commit()

        future = _submit_mainline_cursor_update(writer, "phase3-new", "e" * 40)
        future.result(timeout=5)

        with engine.connect() as c:
            sha = c.execute(
                text(
                    "SELECT commits_walked_to_sha FROM mainline_state "
                    "WHERE tree_name = 'phase3-new'"
                )
            ).scalar_one()
            assert sha == "e" * 40
            c.commit()

            # Cleanup.
            c.execute(text("DELETE FROM mainline_state WHERE tree_name = 'phase3-new'"))
            c.commit()
    finally:
        engine.dispose()
        writer.stop(timeout=5)
