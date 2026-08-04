"""`run_vacuum`'s reporting contract.

Both properties here were wrong in production simultaneously on
2026-08-03, and both were silent: the post-VACUUM truncate no-opped
while the size diff reported the resulting WAL as a 16.7 GB LOSS on an
operation whose purpose is reclaiming space.
"""

import sqlite3

import pytest

from mimir import maintenance


def _make_db(path, rows=2000):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, blob TEXT)")
    conn.executemany(
        "INSERT INTO t (blob) VALUES (?)", [("x" * 400,) for _ in range(rows)]
    )
    conn.commit()
    conn.execute("DELETE FROM t WHERE id % 2 = 0")
    conn.commit()
    conn.close()


@pytest.fixture
def vacuum_db(tmp_path, monkeypatch):
    """Point `run_vacuum` at a throwaway file-backed DB.

    File-backed on purpose: `:memory:` has no WAL and so cannot
    reproduce either property under test.
    """
    db = tmp_path / "v.db"
    _make_db(db)

    class _URL:
        database = str(db)

    class _Engine:
        url = _URL()

        def dispose(self):
            pass

    monkeypatch.setattr(maintenance, "engine", _Engine())
    return db


def test_reclaimed_is_not_inverted_by_the_wal_vacuum_just_wrote(vacuum_db):
    """`reclaimed` must diff the main file, not the file plus its WAL.

    In WAL mode VACUUM's rebuild goes through the WAL, so straight
    afterwards the WAL is ~db_size. Summing it into the "after" total
    made a successful compaction report a large negative number:
    production logged `reclaimed -16720121472 bytes`.

    The fixture MUST hold a second connection open. That is not
    incidental: with nothing blocking it the post-VACUUM truncate
    collapses the WAL, the sum comes out positive anyway, and the test
    passes under the very formula it exists to reject (confirmed by
    mutation). The inflated WAL and the blocked truncate are the same
    production condition, so the guard has to reproduce both.
    """
    holder = sqlite3.connect(str(vacuum_db))
    holder.execute("BEGIN")
    holder.execute("SELECT COUNT(*) FROM t").fetchone()
    try:
        result = maintenance.run_vacuum()
    finally:
        holder.close()

    assert result.wal_truncated is False, "fixture did not block the truncate"
    assert result.db_size_after > result.db_size_before, (
        "fixture did not inflate the WAL, so the summing bug cannot show"
    )
    assert result.reclaimed >= 0, (
        f"a VACUUM that compacted the file reported reclaimed="
        f"{result.reclaimed}; the WAL is being counted as lost space"
    )


def test_a_busy_wal_truncate_is_reported_rather_than_swallowed(vacuum_db):
    """`wal_checkpoint(TRUNCATE)` returns busy=1 and does nothing when
    another connection holds the DB open. It never raises, so an
    unchecked call is invisible: on the live deployment the web and
    tasks containers hold read connections permanently, so the
    post-VACUUM truncate never ran and the WAL stayed at database size
    until the next restart.

    A second open connection is exactly that condition.
    """
    holder = sqlite3.connect(str(vacuum_db))
    holder.execute("BEGIN")
    holder.execute("SELECT COUNT(*) FROM t").fetchone()
    try:
        result = maintenance.run_vacuum()
    finally:
        holder.close()

    assert result.wal_truncated is False, (
        "a truncate blocked by another connection was reported as having "
        "succeeded; that is the silent failure this pins"
    )


def test_an_unobstructed_truncate_reports_success(vacuum_db):
    """The positive half, so the flag cannot be hardwired to False."""
    result = maintenance.run_vacuum()
    assert result.wal_truncated is True
