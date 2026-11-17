"""Tests for mimir/subsystems_dashboard/_path_filter.py: the
shared `_subsystem_path_filter_sql` builder, specifically
the SQL-plan pin that asserts UNION-of-range-seeks (not
the LIKE-with-ESCAPE shape that disabled the index)."""

import pytest
from sqlalchemy import func, select

from mimir.models import Inbox
from mimir.subsystems_dashboard.reads import (
    active_threads_in_subsystem,
    daily_volume_in_subsystem,
)
from mimir.subsystems_dashboard.recent import recent_postings_query
from mimir.subsystems_dashboard.reviewers import active_reviewers_in_subsystem
from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats
from tests.test_subsystems._helpers import (
    _add_recent_patch_with_trailers,
    _add_subsystem,
)


def test_subsystem_path_filter_uses_index_seeks(seeded_db):
    """`_subsystem_path_filter_sql` must emit a plan whose every
    article_files access goes through `ix_article_files_path`, never
    a full SCAN. The earlier shape (`path LIKE :p ESCAPE '\\'`)
    silently disabled SQLite's LIKE→range-scan optimisation and
    collapsed to a full scan of `article_files` per branch (7M rows
    on prod), driving NETWORKING [GENERAL] cold misses to ~10 s
    (#198). Range comparisons (`path >= lo AND path < hi`) keep each
    branch sargable. Pin the plan so the regression is caught at PR
    time instead of in production cold misses.

    Uses both a directory-prefix include and an exclude so the test
    covers the UNION branches and the per-row NOT(exclude) tail."""
    from sqlalchemy import text

    from mimir.subsystems_dashboard._path_filter import _subsystem_path_filter_sql

    with seeded_db() as s:
        sub = _add_subsystem(
            s,
            "NETPLAN",
            "Supported",
            files=["net/", "include/linux/skbuff.h"],
            excludes=["net/bluetooth/"],
        )
        s.commit()
        path_filter = _subsystem_path_filter_sql(sub, prefix="pin")
        assert path_filter is not None
        path_sql, path_params = path_filter
        # Must be a UNION of independent seeks, not OR-of-LIKEs.
        assert " UNION ALL " in path_sql
        assert "LIKE" not in path_sql
        branches = path_sql.split(" UNION ALL ")
        assert len(branches) == 3  # Directory range, bare directory and exact path.
        assert all(
            "article_files INDEXED BY ix_article_files_path" in branch
            for branch in branches
        )
        plan_rows = s.execute(text("EXPLAIN QUERY PLAN " + path_sql), path_params).all()
    plan = "\n".join(r[-1] for r in plan_rows)
    # Every article_files step should be an index SEARCH; no full
    # SCAN article_files should appear anywhere in the plan.
    assert "SCAN article_files" not in plan, (
        f"path filter plan fell back to a full table scan:\n{plan}"
    )
    assert "ix_article_files_path" in plan, (
        f"path filter plan does not use ix_article_files_path:\n{plan}"
    )


@pytest.mark.parametrize(
    "helper",
    [
        daily_volume_in_subsystem,
        active_threads_in_subsystem,
        recent_postings_query,
        active_reviewers_in_subsystem,
    ],
    ids=lambda helper: helper.__name__,
)
def test_path_filter_consumers_use_the_expected_index(seeded_db, helper):
    with seeded_db() as session:
        sub = _add_subsystem(
            session,
            "PATHPLAN",
            "Maintained",
            files=["net/", "include/linux/skbuff.h"],
            excludes=["net/bluetooth/"],
        )
        for n, path in enumerate(
            ("net/a.c", "net", "include/linux/skbuff.h", "net/bluetooth/b.c")
        ):
            article = _add_recent_patch_with_trailers(
                session,
                f"pathplan{n}@x",
                [path],
                [("Reviewed-by", "Reviewer", "reviewer@kernel.org")],
            )
            article.subject = f"[PATCH] Path plan {n}"
        session.commit()
        sid = sub.id
    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as session:
        from mimir.models import Subsystem

        sub = session.get(Subsystem, sid)
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        results = []

        def run():
            if helper is recent_postings_query:
                result = helper(session, inbox, sub)
            else:
                result = helper(session, inbox, sub, force=True)
            results.append(result)
            return result

        plan = plan_of(
            session,
            run,
            "WITH candidates"
            if helper is recent_postings_query
            else "FROM article_files af",
        )
    if helper is recent_postings_query:
        steps = [line for line in plan.splitlines() if "article_files" in line]
        assert len(steps) == 3, plan
        assert all(
            line.startswith("SEARCH article_files USING INDEX ix_article_files_path")
            for line in steps
        ), plan
        assert sum("path>? AND path<?" in line for line in steps) == 1, plan
        assert sum("path=?" in line for line in steps) == 2, plan
    else:
        assert any(
            line.startswith("SEARCH af ")
            and "sqlite_autoindex_article_files_1 (article_id=?" in line
            for line in plan.splitlines()
        ), plan
        assert "ix_article_files_path" not in plan, plan
    assert len(results) == 1
    result = results[0]
    if helper is daily_volume_in_subsystem:
        assert sum(count for _, count in result.days) == 3
    elif helper is active_reviewers_in_subsystem:
        assert [reviewer.total for reviewer in result] == [3]
    else:
        assert len(result) == 3


def test_path_budget_counts_raw_rows_with_a_covering_bounded_scan(seeded_db):
    from mimir.models import ArticleFile, Subsystem
    from mimir.subsystems_dashboard._path_filter import _path_seek_is_bounded
    from tests._helpers import count_steps

    with seeded_db() as session:
        sub = _add_subsystem(
            session, "EXCLUDED BUDGET", None, files=["net/"], excludes=["net/"]
        )
        sid = sub.id
        article = _add_recent_patch_with_trailers(session, "many-paths@x", [], [])
        article_id = article.id
        session.add_all(
            ArticleFile(article_id=article.id, path=f"net/{n:04}.c")
            for n in range(2000)
        )
        session.commit()
    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as session:
        sub = session.get(Subsystem, sid)
        assert len(sub.paths) == 2  # Entire include range is excluded.
        assert (
            session.scalar(
                select(func.count())
                .select_from(ArticleFile)
                .where(ArticleFile.article_id == article_id)
            )
            == 2000
        )
        results = []
        with count_steps(session) as steps:
            plan = plan_of(
                session,
                lambda: results.append(_path_seek_is_bounded(session, sub, 5)),
                "SELECT count(*) FROM (SELECT 1",
            )
        assert results == [False]
        assert steps.count < 1000, steps.count
    assert "COVERING INDEX ix_article_files_path" in plan, plan
    assert "TEMP B-TREE" not in plan, plan
    assert "SCAN article_files" not in plan, plan


def test_path_budget_counts_overlaps_and_exact_boundary(seeded_db):
    from mimir.subsystems_dashboard._path_filter import _path_seek_is_bounded

    with seeded_db() as session:
        sub = _add_subsystem(
            session,
            "OVERLAP BUDGET",
            None,
            files=["net/", "net/a.c"],
            excludes=["net/"],
        )
        _add_recent_patch_with_trailers(session, "overlap@x", ["net/a.c"], [])
        session.commit()
        # One file is reached by two independent seeks, even though X: removes it.
        assert not _path_seek_is_bounded(session, sub, 1)
        assert _path_seek_is_bounded(session, sub, 2)


def test_zero_compound_limit_does_not_disable_bounded_probe(seeded_db):
    import sqlite3

    from mimir.subsystems_dashboard._path_filter import _path_seek_is_bounded

    with seeded_db() as session:
        sub = _add_subsystem(session, "UNLIMITED", None, files=["net/", "drivers/"])
        _add_recent_patch_with_trailers(session, "unlimited@x", ["net/a.c"], [])
        session.commit()
        connection = session.connection().connection.driver_connection
        previous = connection.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 0)
        try:
            # SQLite treats zero as unlimited, not as a ban on compound SELECTs.
            assert _path_seek_is_bounded(session, sub, 1)
        finally:
            connection.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, previous)
