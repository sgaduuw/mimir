"""AGENTS.md check 6 for the series slot rule (#861): every reader that
decides a slot's patch asks `article_files` per row, so each statement
must seek the series key index and the `article_files` primary key under
production statistics, not scan either."""

from datetime import UTC, datetime

from sqlalchemy import select

from mimir.models import Article, ArticleFile, ArticleList, Inbox
from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

KEY_SEEK = "USING INDEX ix_articles_patch_series_key (patch_series_key=?)"
FILES_SEEK = (
    "SEARCH article_files USING COVERING INDEX sqlite_autoindex_article_files_1"
    " (article_id=?)"
)


def _seed_slot(s):
    """A two-row v2 slot after a v1, all covers in alpha, the later v2
    row with a diff. Returns the v2 row to view."""
    alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
    rows = [
        Article(
            message_id=f"plan-{i}@x",
            subject=f"[PATCH {v} 0/1] s",
            author="A <a@x>",
            date=datetime(2024, 6, 1 + i, tzinfo=UTC),
            subject_normalized="s",
            patch_series_key="planslotkey",
            patch_series_version=v,
            patch_series_position=0,
            lists=[ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="aa" * 20)],
        )
        for i, v in enumerate(["v1", "v2", "v2"])
    ]
    rows[2].files = [ArticleFile(path="fs/foo.c")]
    s.add_all(rows)
    s.commit()
    return rows[2]


def _assert_seeks(plan):
    assert KEY_SEEK in plan, plan
    assert FILES_SEEK in plan, plan


def test_the_series_diff_slot_query_seeks_both_indexes(seeded_db):
    from mimir.web.routes.series_diff import _resolve_via_index

    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        _seed_slot(s)
        plan = plan_of(
            s,
            lambda: _resolve_via_index(s, "planslotkey", None, "v1", "v2", 0),
            "patch_series_version IN",
        )
    _assert_seeks(plan)


def test_the_revisions_fold_query_seeks_both_indexes(seeded_db):
    from mimir.patch_state import _series_rows

    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        viewed = _seed_slot(s)
        plan = plan_of(s, lambda: _series_rows(s, viewed), "article_files")
    _assert_seeks(plan)


def test_the_sitemap_bucket_query_seeks_both_indexes(seeded_db):
    from mimir.seo.sitemaps import series_diff_sitemap_xml

    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        _seed_slot(s)
        plan = plan_of(
            s,
            lambda: series_diff_sitemap_xml(s, "p", "", force=True),
            "patch_series_version",
        )
    assert (
        "ix_articles_patch_series_key (patch_series_key>? AND patch_series_key<?)"
        in plan
    ), plan
    assert FILES_SEEK in plan, plan


def test_the_validator_series_query_seeks_both_indexes(seeded_db):
    from mimir.web.routes._validators import render_state_tag

    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        viewed = _seed_slot(s)
        plan = plan_of(
            s,
            lambda: render_state_tag(s, viewed.id, viewed.message_id),
            "EXISTS",
        )
    _assert_seeks(plan)
