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
