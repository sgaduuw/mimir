"""`tests._helpers.planner_stats` puts back what it found (#1161)."""

from sqlalchemy import text

from tests._helpers import PRODUCTION_STATS, planner_stats


def _table_exists(session_factory):
    with session_factory() as s:
        return bool(
            s.execute(
                text("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'")
            ).first()
        )


def _drop_stats(session_factory):
    with session_factory() as s:
        s.execute(text("DROP TABLE IF EXISTS sqlite_stat1"))
        s.commit()
        s.get_bind().dispose()


def test_planner_stats_leaves_no_table_when_there_was_none(seeded_db):
    _drop_stats(seeded_db)
    with planner_stats(seeded_db, PRODUCTION_STATS):
        assert _table_exists(seeded_db)
    assert not _table_exists(seeded_db)


def test_planner_stats_restores_existing_rows_exactly(seeded_db):
    before = [("articles", "ix_articles_date", "10 1")]
    _drop_stats(seeded_db)
    # The outer block pins `before`; the inner one must put it back.
    with planner_stats(seeded_db, before):
        with planner_stats(seeded_db, PRODUCTION_STATS):
            pass
        with seeded_db() as s:
            rows = [tuple(r) for r in s.execute(text("SELECT * FROM sqlite_stat1"))]
    assert rows == before
    _drop_stats(seeded_db)
