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


def test_the_lifecycle_pill_query_seeks_the_files_index(seeded_db):
    """#949: the pill's superseding copy asks `article_files` per candidate
    row, on every listing page, so that EXISTS must seek the primary key
    index (covering) under production statistics."""
    from mimir.lifecycle_status import _bulk_uncached

    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        viewed = _seed_slot(s)
        inbox_id = s.scalar(select(Inbox.id).where(Inbox.name == "alpha"))
        plan = plan_of(s, lambda: _bulk_uncached(s, [viewed.id], inbox_id), "sup_by_id")
    assert FILES_SEEK.replace("article_files", "f", 1) in plan, plan


def _seed_posting(s):
    """A v2 cover with three patches in alpha, the second posted twice,
    one landed and one reviewed. Returns the cover."""
    from mimir.models import ArticleTrailer, MainlineCommit

    alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
    cover = None
    rows = []
    for n, suffix in [(0, ""), (1, ""), (2, ""), (2, "b"), (3, "")]:
        a = Article(
            message_id=f"roll-{n}{suffix}@x",
            subject=f"[PATCH v2 {n}/3] s",
            author="A <a@x>",
            date=datetime(2024, 6, 1 + n, tzinfo=UTC),
            subject_normalized="s",
            patch_series_key="rollkey",
            patch_series_version="v2",
            patch_series_position=n,
            lists=[ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="aa" * 20)],
        )
        rows.append(a)
        cover = cover or a
    rows[1].files = [ArticleFile(path="fs/foo.c")]
    s.add_all(rows)
    s.flush()
    s.add(
        MainlineCommit(
            commit_sha="1" * 40,
            message_id=rows[1].message_id,
            tree_name="linus",
            committed_at=datetime(2024, 7, 1, tzinfo=UTC),
        )
    )
    s.add(
        ArticleTrailer(
            article_id=rows[2].id,
            role="Reviewed-by",
            name="R",
            address="r@x",
            address_normalized="r@x",
        )
    )
    s.commit()
    return cover


def test_the_cover_rollup_probes_seek_their_indexes(seeded_db):
    """#938: the roll-up runs inside the listing statement, once per cover
    of the page, so every probe must be a seek under production
    statistics and `articles` must not be scanned for it."""
    from mimir.lifecycle_status import _bulk_uncached

    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        cover = _seed_posting(s)
        inbox_id = s.scalar(select(Inbox.id).where(Inbox.name == "alpha"))
        plan = plan_of(s, lambda: _bulk_uncached(s, [cover.id], inbox_id), "sup_by_id")
    rollup = plan.split("MATERIALIZE rollup")[1].split("MATERIALIZE rc")[0]
    assert "SEARCH p USING INDEX ix_articles_patch_series_key" in rollup, rollup
    assert FILES_SEEK.replace("article_files", "f", 1) in rollup, rollup
    assert "SEARCH m USING INDEX ix_mainline_commits_message_id" in rollup, rollup
    assert "ix_mainline_commits_tree_name" not in rollup, rollup
    assert "SEARCH t USING INDEX ix_article_trailers_article_id" in rollup, rollup
    assert "SEARCH a2 USING INDEX ix_articles_patch_series_key" in rollup, rollup
    # The posting's co-routine is a bounded result, not the table.
    scans = {ln.strip().split(" ")[1] for ln in rollup.splitlines() if "SCAN " in ln}
    assert not scans & {"p", "c", "a2"}, rollup


def test_a_covers_validator_reads_the_posting_through_indexes(seeded_db):
    """#938: for a cover the landings and trailers the validator reads are
    the posting's, in the same statements, and must stay seeks: this runs
    before every 304."""
    from mimir.web.routes._validators import render_state_tag

    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        cover = _seed_posting(s)
        s.expire_all()
        args = (s, cover.id, cover.message_id)
        landings = plan_of(s, lambda: render_state_tag(*args), "FROM mainline_commits")
        trailers = plan_of(s, lambda: render_state_tag(*args), "FROM article_trailers")
    assert "ix_mainline_commits_message_id" in landings, landings
    assert "SCAN mainline_commits" not in landings, landings
    assert "ix_article_trailers_article_id" in trailers, trailers
    assert "SCAN article_trailers" not in trailers, trailers
    for plan in (landings, trailers):
        assert "ix_articles_patch_series_key" in plan, plan
