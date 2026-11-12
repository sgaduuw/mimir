"""`tests._helpers.planner_stats` puts back what it found (#1161)."""

import pytest
from sqlalchemy import text

from tests._helpers import PRODUCTION_STATS, planner_stats


def _rows(session_factory, table):
    with session_factory() as s:
        return sorted(tuple(r) for r in s.execute(text(f"SELECT * FROM {table}")))


def test_planner_stats_restores_existing_stat1_rows_exactly(seeded_db):
    with planner_stats(seeded_db, [("articles", "ix_articles_date", "10 1")]):
        pinned = _rows(seeded_db, "sqlite_stat1")
        # The inner block must put the outer block's rows back.
        with planner_stats(seeded_db, PRODUCTION_STATS):
            pass
        assert _rows(seeded_db, "sqlite_stat1") == pinned
    assert pinned == [("articles", "ix_articles_date", "10 1")]


def test_planner_stats_restores_sqlite_stat4(seeded_db):
    """Every ANALYZE inside `planner_stats` refills `sqlite_stat4` with
    samples of the test's own data; they must not outlive the block."""
    with planner_stats(seeded_db):
        with seeded_db() as s:
            has_stat4 = s.execute(
                text("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat4'")
            ).first()
        if not has_stat4:
            pytest.skip("SQLite built without STAT4")
        with seeded_db() as s:
            s.execute(text("ANALYZE"))
            s.commit()
        before = _rows(seeded_db, "sqlite_stat4")
        assert before, "ANALYZE sampled nothing, the test would prove nothing"
        with planner_stats(seeded_db, PRODUCTION_STATS):
            assert _rows(seeded_db, "sqlite_stat4") == []
        assert _rows(seeded_db, "sqlite_stat4") == before


@pytest.mark.parametrize("fails", [False, True])
def test_preserve_mode_restores_analyze_statistics(seeded_db, fails):
    """The autouse scope must restore real samples even on a failing test."""
    with planner_stats(seeded_db, PRODUCTION_STATS):
        before = _rows(seeded_db, "sqlite_stat1")
        with seeded_db() as s:
            has_stat4 = s.execute(
                text("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat4'")
            ).first()
        before4 = _rows(seeded_db, "sqlite_stat4") if has_stat4 else []
        try:
            with planner_stats(seeded_db, rows=None):
                assert _rows(seeded_db, "sqlite_stat1") == before
                with seeded_db() as s:
                    s.execute(text("ANALYZE"))
                    s.commit()
                assert _rows(seeded_db, "sqlite_stat1") != before
                if has_stat4:
                    assert _rows(seeded_db, "sqlite_stat4"), "ANALYZE sampled nothing"
                if fails:
                    raise ValueError("test failed after ANALYZE")
        except ValueError:
            assert fails
        assert _rows(seeded_db, "sqlite_stat1") == before
        if has_stat4:
            assert _rows(seeded_db, "sqlite_stat4") == before4


def test_preserve_mode_skips_restore_when_statistics_are_unchanged(seeded_db):
    from sqlalchemy import event

    with seeded_db() as s:
        engine = s.get_bind()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    original_pool = engine.pool
    event.listen(engine, "before_cursor_execute", record)
    try:
        with planner_stats(seeded_db, rows=None):
            pass
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert engine.pool is not original_pool
    assert statements, "the scope ran no statements"
    assert all(
        statement.lstrip().upper().startswith("SELECT") for statement in statements
    )


def test_preserve_mode_discards_plans_from_a_rolled_back_analyze(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'stats.db'}")
    sessions = sessionmaker(bind=engine)
    query = "EXPLAIN QUERY PLAN SELECT * FROM probe WHERE x=1"
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE probe(x, payload)")
            connection.exec_driver_sql("CREATE INDEX probe_x ON probe(x)")
            connection.exec_driver_sql(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL "
                "SELECT x+1 FROM n WHERE x<10000) "
                "INSERT INTO probe SELECT 1,x FROM n"
            )
            connection.exec_driver_sql("ANALYZE")
        with planner_stats(sessions, [("probe", "probe_x", "10000 1")]):
            original = _rows(sessions, "sqlite_stat1")
            with sessions() as session:
                before = session.execute(text(query)).all()
            assert "SEARCH probe USING INDEX probe_x" in before[0][3]

            with planner_stats(sessions, rows=None):
                with sessions() as session:
                    # sqlite3 legacy transaction mode otherwise autocommits ANALYZE.
                    session.execute(text("BEGIN"))
                    session.execute(text("ANALYZE"))
                    assert session.execute(text(query)).one()[3] == "SCAN probe"
                    session.rollback()
                assert _rows(sessions, "sqlite_stat1") == original

            with sessions() as session:
                after = session.execute(text(query)).all()
            assert after == before
    finally:
        engine.dispose()


def test_preserve_mode_accepts_table_statistics_with_a_partial_index(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'partial.db'}")
    sessions = sessionmaker(bind=engine)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE probe(x)")
            connection.exec_driver_sql("CREATE INDEX probe_x ON probe(x) WHERE x=1")
            connection.exec_driver_sql("INSERT INTO probe VALUES (1), (2)")
            connection.exec_driver_sql("ANALYZE")
        with sessions() as session:
            rows = session.execute(text("SELECT * FROM sqlite_stat1")).all()
        assert {row.idx for row in rows} == {"probe_x", None}
        with planner_stats(sessions, rows=None):
            pass
        with sessions() as session:
            assert session.execute(text("SELECT * FROM sqlite_stat1")).all() == rows
    finally:
        engine.dispose()
