"""Tests for mimir/subsystems_dashboard/_path_filter.py: the
shared `_subsystem_path_filter_sql` builder, specifically
the SQL-plan pin that asserts UNION-of-range-seeks (not
the LIKE-with-ESCAPE shape that disabled the index)."""





from tests.test_subsystems._helpers import _add_subsystem


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
            s, "NETPLAN", "Supported",
            files=["net/", "include/linux/skbuff.h"],
            excludes=["net/bluetooth/"],
        )
        s.commit()
        path_filter = _subsystem_path_filter_sql(sub, prefix="pin")
        assert path_filter is not None
        path_sql, path_params = path_filter
        # Must be a UNION of independent seeks, not OR-of-LIKEs.
        assert " UNION " in path_sql
        assert "LIKE" not in path_sql
        plan_rows = s.execute(
            text("EXPLAIN QUERY PLAN " + path_sql), path_params
        ).all()
    plan = "\n".join(r[-1] for r in plan_rows)
    # Every article_files step should be an index SEARCH; no full
    # SCAN article_files should appear anywhere in the plan.
    assert "SCAN article_files" not in plan, (
        f"path filter plan fell back to a full table scan:\n{plan}"
    )
    assert "ix_article_files_path" in plan, (
        f"path filter plan does not use ix_article_files_path:\n{plan}"
    )
