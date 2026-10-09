"""Guards for migration mechanics that the ORM-level suite cannot see.

The rest of the suite runs against a database already at head, so it
says nothing about how a migration behaves when a previous attempt was
interrupted. On SQLite that is not a theoretical concern: alembic runs
DDL non-transactionally, and any `batch_alter_table` that adds a
constraint becomes a move-and-copy rebuild, so an interrupted run leaves
real intermediate states on disk.

These tests build those states directly and run the real migration
against them, rather than reasoning about what alembic would do.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PREV_REVISION = "e3aa78c72a8d"
THREAD_ROOT_REVISION = "1072ad1fae96"
TREE_KEY_REVISION = "13b7f3111e00"


def _alembic(
    db_path: Path, target: str, command: str = "upgrade"
) -> subprocess.CompletedProcess:
    """Run a real `alembic upgrade` (or `downgrade`) against a throwaway database.

    A subprocess with `DATABASE_URL` overridden, never the ambient
    session: `alembic downgrade`/`upgrade` against the default URL would
    target the developer's own database, and a downgrade there is
    destructive and irreversible.

    The environment is inherited rather than enumerated. An enumerated
    env has to list every variable `Settings` requires, so it breaks the
    moment one is added, and it broke immediately: it omitted
    `SECRET_KEY`, which is required with no default. Locally that was
    invisible because the subprocess reads `.env` from `cwd`; CI has no
    `.env` and passes the value as a job-level env var, so both tests
    errored there and nowhere else. `SECRET_KEY` is still pinned below
    so the test does not depend on either source existing.
    """
    return subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "SECRET_KEY": "migration-test-key-not-for-real-use",
            "DATABASE_URL": f"sqlite:///{db_path}",
            # Migrations write, and every non-broker connection is
            # opened `query_only=1`.
            "MIMIR_IS_BROKER": "true",
        },
    )


def _seed(db_path: Path, rows: int = 200) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO inboxes(id,name,mirror_path,upstream_url) "
            "VALUES (1,'a','/m','u')"
        )
        conn.executemany(
            "INSERT INTO articles(id,message_id,subject_normalized) VALUES (?,?,?)",
            [(i, f"m{i}@x", "s") for i in range(1, rows + 1)],
        )
        conn.executemany(
            "INSERT INTO article_lists(article_id,inbox_id,epoch,commit_sha) "
            "VALUES (?,1,'0.git',?)",
            [(i, f"{i:040x}") for i in range(1, rows + 1)],
        )
        conn.commit()
    finally:
        conn.close()


def _make_scratch(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE _alembic_tmp_article_lists (
               article_id INTEGER NOT NULL, inbox_id INTEGER NOT NULL,
               epoch VARCHAR NOT NULL, commit_sha VARCHAR NOT NULL,
               thread_root_id INTEGER,
               PRIMARY KEY (article_id, inbox_id))"""
    )


def _indexes(conn: sqlite3.Connection) -> set[str]:
    """The non-implicit indexes on `article_lists`.

    Asserted explicitly because batch mode rebuilds indexes from
    REFLECTION: it restores only what the table it replaced happened to
    carry, so an index can vanish while the migration reports success.
    """
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='article_lists' AND name NOT LIKE 'sqlite_%'"
        )
    }


EXPECTED_INDEXES = {"ix_article_lists_inbox_id", "ix_article_lists_thread_root"}


@pytest.fixture
def staged_db(tmp_path):
    """A throwaway database one revision below the thread-root rebuild."""
    db = tmp_path / "staged.db"
    result = _alembic(db, PREV_REVISION)
    assert result.returncode == 0, result.stderr[-2000:]
    _seed(db)
    return db


def test_interrupted_copy_is_cleaned_up_and_the_migration_completes(staged_db):
    """State (a): killed during the `INSERT ... SELECT`.

    Both tables exist and the scratch one is a partial duplicate, so
    dropping it is right. Without this the retry dies on `table
    _alembic_tmp_article_lists already exists`, which under
    `Restart=always` is a permanent crash-loop rather than a transient
    failure.
    """
    conn = sqlite3.connect(staged_db)
    try:
        _make_scratch(conn)
        conn.execute(
            "INSERT INTO _alembic_tmp_article_lists "
            "SELECT article_id,inbox_id,epoch,commit_sha,NULL "
            "FROM article_lists LIMIT 50"
        )
        conn.commit()
    finally:
        conn.close()

    result = _alembic(staged_db, THREAD_ROOT_REVISION)
    assert result.returncode == 0, result.stderr[-2000:]

    conn = sqlite3.connect(staged_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM article_lists").fetchone()[0] == 200
        cols = [r[1] for r in conn.execute("PRAGMA table_info(article_lists)")]
        assert "thread_root_id" in cols
        assert _indexes(conn) == EXPECTED_INDEXES
        leftovers = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp%'"
            )
        ]
        assert leftovers == [], f"scratch table left behind: {leftovers}"
    finally:
        conn.close()


def test_migration_refuses_to_drop_the_scratch_table_when_it_is_the_only_copy(
    staged_db,
):
    """State (b): killed between alembic's DROP and its RENAME.

    The scratch table holds every row and `article_lists` is gone, so an
    unconditional `DROP TABLE IF EXISTS _alembic_tmp_article_lists`
    destroys the table outright. That is not hypothetical: the first
    version of this migration's cleanup did exactly that, and against
    this fixture it left zero surviving rows before the guard existed.

    The migration must refuse and say how to recover, rather than
    auto-repairing: completing the rename would also have to rebuild the
    indexes the interrupted run never reached and reconcile
    `alembic_version`, on a table that is 28.8M rows in production.
    """
    conn = sqlite3.connect(staged_db)
    try:
        _make_scratch(conn)
        conn.execute(
            "INSERT INTO _alembic_tmp_article_lists "
            "SELECT article_id,inbox_id,epoch,commit_sha,NULL FROM article_lists"
        )
        conn.execute("DROP TABLE article_lists")
        conn.commit()
    finally:
        conn.close()

    result = _alembic(staged_db, THREAD_ROOT_REVISION)
    assert result.returncode != 0, "migration should refuse, not proceed"
    assert "ONLY copy of the data" in result.stderr, (
        f"expected the data-loss guard to fire; got: {result.stderr[-2000:]}"
    )

    conn = sqlite3.connect(staged_db)
    try:
        surviving = conn.execute(
            "SELECT COUNT(*) FROM _alembic_tmp_article_lists"
        ).fetchone()[0]
        assert surviving == 200, (
            f"the only copy of the data was destroyed: {surviving} rows survive"
        )
        # Apply the one statement the message prescribes, and nothing
        # else: no indexes, no `alembic stamp`.
        conn.execute("ALTER TABLE _alembic_tmp_article_lists RENAME TO article_lists")
        conn.commit()
    finally:
        conn.close()

    # Restarting must now complete the migration by itself. This is the
    # half-applied state that used to exit 0 while silently dropping
    # `ix_article_lists_inbox_id` for good: batch mode rebuilds indexes
    # from reflection, and the renamed scratch table carries none.
    result = _alembic(staged_db, THREAD_ROOT_REVISION)
    assert result.returncode == 0, result.stderr[-2000:]

    conn = sqlite3.connect(staged_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM article_lists").fetchone()[0] == 200
        cols = [r[1] for r in conn.execute("PRAGMA table_info(article_lists)")]
        assert "thread_root_id" in cols
        assert _indexes(conn) == EXPECTED_INDEXES
    finally:
        conn.close()


def test_migration_completes_when_indexes_were_recreated_by_hand(staged_db):
    """An operator who ran the pre-3.7.0 recovery recipe must not be stuck.

    That recipe told them to create both indexes and then `alembic
    stamp`. Someone who does the first half and restarts before the
    stamp re-runs this migration against a table that already has both
    indexes, which used to die on `index ... already exists`. Under
    `Restart=always` with the web tier held behind `Requires=`, that is a
    crash-loop, not a failed command.
    """
    conn = sqlite3.connect(staged_db)
    try:
        _make_scratch(conn)
        conn.execute(
            "INSERT INTO _alembic_tmp_article_lists "
            "SELECT article_id,inbox_id,epoch,commit_sha,NULL FROM article_lists"
        )
        conn.execute("DROP TABLE article_lists")
        conn.execute("ALTER TABLE _alembic_tmp_article_lists RENAME TO article_lists")
        conn.execute(
            "CREATE INDEX ix_article_lists_inbox_id ON article_lists (inbox_id)"
        )
        conn.execute(
            "CREATE INDEX ix_article_lists_thread_root "
            "ON article_lists (inbox_id, thread_root_id)"
        )
        conn.commit()
    finally:
        conn.close()

    result = _alembic(staged_db, THREAD_ROOT_REVISION)
    assert result.returncode == 0, result.stderr[-2000:]

    conn = sqlite3.connect(staged_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM article_lists").fetchone()[0] == 200
        assert _indexes(conn) == EXPECTED_INDEXES
    finally:
        conn.close()


# mainline_commits key rebuild (#673).

_MC_ROWS = [
    # Same commit recorded by a subsystem tree and by Linus: the case the
    # old key could not hold. Inserted subsystem-first, as in production.
    ("a" * 40, "m1@x", "net-next", "2026-01-01 00:00:00"),
    ("b" * 40, "m2@x", "tip", "2026-01-02 00:00:00"),
]


def _mc_rows(conn: sqlite3.Connection) -> list[tuple]:
    return sorted(
        conn.execute("SELECT commit_sha, message_id, tree_name FROM mainline_commits")
    )


def _mc_indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='mainline_commits' AND name NOT LIKE 'sqlite_%'"
        )
    }


MC_INDEXES = {"ix_mainline_commits_message_id", "ix_mainline_commits_tree_name"}


@pytest.fixture
def mc_db(tmp_path):
    """A throwaway database one revision below the key rebuild, with rows."""
    db = tmp_path / "mc.db"
    result = _alembic(db, THREAD_ROOT_REVISION)
    assert result.returncode == 0, result.stderr[-2000:]
    conn = sqlite3.connect(db)
    try:
        conn.executemany("INSERT INTO mainline_commits VALUES (?,?,?,?)", _MC_ROWS)
        conn.commit()
    finally:
        conn.close()
    return db


def _linus_row_for_m1(db: Path) -> sqlite3.IntegrityError | None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO mainline_commits VALUES (?,?,?,?)",
            ("a" * 40, "m1@x", "linus", "2026-02-01 00:00:00"),
        )
        conn.commit()
        return None
    except sqlite3.IntegrityError as exc:
        return exc
    finally:
        conn.close()


def test_tree_key_upgrade_keeps_rows_and_admits_a_second_tree(mc_db):
    # Precondition: the old key refuses the Linus row for an owned commit.
    assert _linus_row_for_m1(mc_db) is not None

    result = _alembic(mc_db, TREE_KEY_REVISION)
    assert result.returncode == 0, result.stderr[-2000:]

    assert _linus_row_for_m1(mc_db) is None
    conn = sqlite3.connect(mc_db)
    try:
        assert _mc_rows(conn) == [
            ("a" * 40, "m1@x", "linus"),
            ("a" * 40, "m1@x", "net-next"),
            ("b" * 40, "m2@x", "tip"),
        ]
        assert _mc_indexes(conn) == MC_INDEXES
    finally:
        conn.close()


def test_tree_key_downgrade_keeps_the_linus_row(mc_db):
    assert _alembic(mc_db, TREE_KEY_REVISION).returncode == 0
    assert _linus_row_for_m1(mc_db) is None

    result = _alembic(mc_db, THREAD_ROOT_REVISION, command="downgrade")
    assert result.returncode == 0, result.stderr[-2000:]

    conn = sqlite3.connect(mc_db)
    try:
        assert _mc_rows(conn) == [
            ("a" * 40, "m1@x", "linus"),
            ("b" * 40, "m2@x", "tip"),
        ]
        assert _mc_indexes(conn) == MC_INDEXES
    finally:
        conn.close()


def test_tree_key_rebuild_clears_debris_but_never_the_only_copy(mc_db):
    conn = sqlite3.connect(mc_db)
    try:
        conn.execute("CREATE TABLE _mainline_commits_rebuild (x INTEGER)")
        conn.commit()
    finally:
        conn.close()
    result = _alembic(mc_db, TREE_KEY_REVISION)
    assert result.returncode == 0, result.stderr[-2000:]
    conn = sqlite3.connect(mc_db)
    try:
        assert len(_mc_rows(conn)) == 2
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='_mainline_commits_rebuild'"
        ).fetchone()

        # Scratch holds the only copy: the migration must refuse.
        conn.execute("ALTER TABLE mainline_commits RENAME TO _mainline_commits_rebuild")
        conn.execute(
            "UPDATE alembic_version SET version_num = ?", (THREAD_ROOT_REVISION,)
        )
        conn.commit()
    finally:
        conn.close()
    result = _alembic(mc_db, TREE_KEY_REVISION)
    assert result.returncode != 0
    assert "ONLY copy of the data" in result.stderr
    conn = sqlite3.connect(mc_db)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM _mainline_commits_rebuild").fetchone()[0]
            == 2
        )
    finally:
        conn.close()
