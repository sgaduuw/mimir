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


def _sizes(db):
    """(main, wal, shm) byte sizes for `db`, mirroring `_db_sizes`."""
    from pathlib import Path

    out = []
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        out.append(p.stat().st_size if p.exists() else 0)
    return tuple(out)


def _true_reclaim(db, main_before: int) -> int:
    """The space the VACUUM really freed.

    Measured the only way it can be: with nothing holding the database,
    collapse the WAL into the main file and diff. Independent of
    `run_vacuum`'s own arithmetic, so it cannot agree with a bug by
    construction the way a re-derivation of the same formula would.
    """
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0, (
            "the reference checkpoint was itself blocked; the measurement "
            "below would understate the true reclaim"
        )
    finally:
        conn.close()
    return main_before - _sizes(db)[0]


def test_reclaimed_reports_the_space_freed_when_the_truncate_was_blocked(vacuum_db):
    """`reclaimed` must be the space the VACUUM freed, on the shape
    production always has.

    In WAL mode VACUUM writes the rebuilt database into the WAL; the
    main file is only rewritten when a checkpoint copies it across, and
    a checkpoint cannot advance past the oldest reader's snapshot. So
    when another connection holds the database open (the live-deployment
    condition, per this branch's own changelog: the web and tasks
    containers hold read connections continuously), the compacted
    content sits entirely in the WAL and the main file does not move at
    all. Diffing the main file then yields exactly 0 for an operation
    that freed most of the database.

    Measured on this fixture (2026-09-26): the VACUUM freed 454,656 of
    954,368 bytes and `reclaimed` came back 0. Same shape at a larger
    scale on a 200k-row throwaway DB: 82,120,704 of 91,258,880 freed,
    `reclaimed` 0. The pre-change formula reported the same event
    as a large NEGATIVE number (production logged
    -16,720,121,472 on 2026-08-03). Both are wrong, and the sibling
    guard above cannot tell them apart, because `reclaimed >= 0` is
    satisfied by construction when `reclaimed` is 0: the failure state
    IS zero. Hence this test asserts the value, not its sign.

    Summing the WAL back in is not the fix: that is the pre-change
    formula, and it reports the compaction as a loss again. The
    measurement that holds while a reader is parked is the LOGICAL
    database size, `PRAGMA page_count * PRAGMA page_size` on the
    vacuuming connection, before and after the `VACUUM`. Freelist pages
    count toward `page_count`, so before is the bloated size and after
    is the compact one, and neither depends on whether a checkpoint
    landed. Verified on the 200k-row DB: 91,258,880 -> 9,138,176, i.e.
    82,120,704, exactly the main file's eventual shrink once the WAL is
    finally collapsed.
    """
    main_before, wal_before, _ = _sizes(vacuum_db)

    holder = sqlite3.connect(str(vacuum_db))
    holder.execute("BEGIN")
    holder.execute("SELECT COUNT(*) FROM t").fetchone()
    try:
        result = maintenance.run_vacuum()
    finally:
        holder.close()

    assert result.wal_truncated is False, "fixture did not block the truncate"
    assert wal_before == 0, "fixture left a pre-existing WAL; sizes are not comparable"

    truth = _true_reclaim(vacuum_db, main_before)
    assert truth > 0, (
        "fixture VACUUM freed nothing, so the assertion below would be vacuous"
    )
    assert result.reclaimed >= truth * 0.9, (
        f"VACUUM freed {truth} bytes; run_vacuum reported "
        f"reclaimed={result.reclaimed}. On the shape production always "
        f"has, the main-file diff sees none of the compaction."
    )


def test_the_operator_summary_reconciles_its_own_three_numbers(vacuum_db, capsys):
    """before, after and reclaimed must share one measurement basis.

    `mimir vacuum` prints the three lines together. What this fixture
    actually produces (2026-09-26):

        before: total=932.0 KB
        after:  total=1.4 MB
        reclaimed 0.0 B in 5.4 s

    `db_size_before` / `db_size_after` sum main+wal+shm while
    `reclaimed` diffs the main file alone, so the three no longer form
    an identity and an operator cannot derive any of them from the
    others. It reads as "the database grew, and nothing was reclaimed"
    for a VACUUM that freed just under half the file (and 90% of it on
    the 200k-row variant). That is not an improvement on the negative
    number the branch
    set out to fix; it is the same defect (a figure whose basis is not
    the one its label implies) with a different sign.

    Either fix satisfies this: report all three on the main+wal basis,
    or report all three on the main-file basis. What must not ship is
    two lines on one basis and the third on another.
    """
    holder = sqlite3.connect(str(vacuum_db))
    holder.execute("BEGIN")
    holder.execute("SELECT COUNT(*) FROM t").fetchone()
    try:
        result = maintenance.run_vacuum()
    finally:
        holder.close()

    from mimir.cli.maintenance import _echo_vacuum_outcome

    _echo_vacuum_outcome(result.model_dump(mode="json"))
    printed = capsys.readouterr().out

    assert "before: total=" in printed and "reclaimed " in printed
    assert result.db_size_before - result.db_size_after == result.reclaimed, (
        f"the summary prints before={result.db_size_before} "
        f"after={result.db_size_after} reclaimed={result.reclaimed}; "
        f"before - after = {result.db_size_before - result.db_size_after}, "
        f"which is not the reclaimed figure on the same lines.\n{printed}"
    )
