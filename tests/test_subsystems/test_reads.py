"""Tests for mimir/subsystems_dashboard/reads.py: per-
subsystem read fan-outs that power the subsystem
dashboard (`recent_articles_in_subsystem`,
`active_threads_in_subsystem`, `daily_volume_in_subsystem`)."""

from datetime import datetime, timezone

from sqlalchemy import select

from mimir.models import (
    Article,
    ArticleFile,
    Inbox,
)
from mimir.subsystems_dashboard import (
    active_threads_in_subsystem,
    daily_volume_in_subsystem,
    recent_articles_in_subsystem,
)

from tests.test_subsystems._helpers import (
    _add_patch_article,
    _add_recent_thread_root,
    _add_subsystem,
)


def test_recent_articles_in_subsystem_basic_match(seeded_db):
    """Articles whose paths match the subsystem's F: globs surface
    in the dashboard list; non-matching articles don't."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_patch_article(s, "in1@x", ["fs/bcachefs/super.c"])
        _add_patch_article(s, "in2@x", ["fs/bcachefs/io.c"])
        _add_patch_article(s, "out@x", ["fs/btrfs/extent.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = recent_articles_in_subsystem(s, alpha, sub)
    msgids = {p.message_id for p in out}
    assert msgids == {"in1@x", "in2@x"}


def test_recent_articles_in_subsystem_is_cached(seeded_db):
    """The helper is the dominant cold-load cost of the per-subsystem
    dashboard (joins article_files × article_lists × articles with one
    OR clause per F: glob, then a Python X: pass). A repeated call
    must short-circuit through the cache instead of re-running the
    join; a `force=True` call must bypass."""
    from mimir import cache
    from mimir.cache import _ns
    from mimir.models import CacheEntry
    from sqlalchemy import delete as sql_delete

    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_patch_article(s, "cache-hit@x", ["fs/bcachefs/super.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()

        # Clean any prior cache row for this (inbox, subsystem, limit).
        nskey = _ns(f"recent_articles_in_subsystem:alpha:{sub.id}:20")
        s.execute(sql_delete(CacheEntry).where(CacheEntry.key == nskey))
        s.commit()

        # First call computes + caches.
        first = recent_articles_in_subsystem(s, alpha, sub)
        assert {p.message_id for p in first} == {"cache-hit@x"}

        # Cache row landed.
        assert cache.get(f"recent_articles_in_subsystem:alpha:{sub.id}:20") is not None

        # Add a NEW matching article that would surface if the helper
        # recomputed. Without force the cache hides it.
        _add_patch_article(s, "added-after@x", ["fs/bcachefs/io.c"])
        s.commit()
        second = recent_articles_in_subsystem(s, alpha, sub)
        assert {p.message_id for p in second} == {"cache-hit@x"}, (
            f"expected cache hit returning stale row; got recompute: {second}"
        )

        # force=True bypasses and picks up the new article.
        third = recent_articles_in_subsystem(s, alpha, sub, force=True)
        assert {p.message_id for p in third} == {"cache-hit@x", "added-after@x"}


def test_recent_articles_in_subsystem_respects_exclude_globs(seeded_db):
    """A subsystem's X: globs veto articles whose only matched
    paths are excluded. The dashboard mirrors the patch-page
    header semantics."""
    with seeded_db() as s:
        sub = _add_subsystem(
            s,
            "BTRFS-MAIN",
            "Maintained",
            files=["fs/btrfs/"],
            excludes=["fs/btrfs/tests/"],
        )
        _add_patch_article(s, "main@x", ["fs/btrfs/extent.c"])
        _add_patch_article(s, "tests@x", ["fs/btrfs/tests/runner.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = recent_articles_in_subsystem(s, alpha, sub)
    assert {p.message_id for p in out} == {"main@x"}


def test_recent_articles_in_subsystem_keeps_article_with_one_in_scope_path(
    seeded_db,
):
    """An article touching both an included and an excluded path
    still belongs to the subsystem, the X: pass only vetoes
    articles whose paths are *all* excluded."""
    with seeded_db() as s:
        sub = _add_subsystem(
            s,
            "BTRFS-MAIN",
            "Maintained",
            files=["fs/btrfs/"],
            excludes=["fs/btrfs/tests/"],
        )
        _add_patch_article(
            s,
            "mixed@x",
            [
                "fs/btrfs/extent.c",
                "fs/btrfs/tests/runner.c",
            ],
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = recent_articles_in_subsystem(s, alpha, sub)
    assert {p.message_id for p in out} == {"mixed@x"}


def test_recent_articles_in_subsystem_scoped_to_inbox(seeded_db):
    """Articles linked only to the other inbox don't appear."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_patch_article(s, "in-alpha@x", ["fs/bcachefs/super.c"], inbox_name="alpha")
        _add_patch_article(s, "in-beta@x", ["fs/bcachefs/io.c"], inbox_name="beta")
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        out_alpha = recent_articles_in_subsystem(s, alpha, sub)
        out_beta = recent_articles_in_subsystem(s, beta, sub)
    assert {p.message_id for p in out_alpha} == {"in-alpha@x"}
    assert {p.message_id for p in out_beta} == {"in-beta@x"}


def test_recent_articles_in_subsystem_orders_by_date_desc(seeded_db):
    """Newest articles first, operator wants "what's been happening
    in this subsystem lately" at the top."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        from mimir.models import ArticleList

        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        for i, day in enumerate([10, 5, 15]):
            art = Article(
                message_id=f"d{i}@x",
                subject=f"patch {i}",
                author="a@x",
                date=datetime(2024, 6, day, tzinfo=timezone.utc),
                thread_parent=None,
                subject_normalized=f"patch {i}",
                canonical_inbox_id=alpha.id,
                lists=[
                    ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="f" * 40)
                ],
                files=[ArticleFile(path="fs/bcachefs/super.c")],
            )
            s.add(art)
        s.commit()
        out = recent_articles_in_subsystem(s, alpha, sub)
    assert [p.date.day for p in out] == [15, 10, 5]


def test_recent_articles_in_subsystem_exact_path_glob(seeded_db):
    """A non-directory F: rule (e.g. `Documentation/foo.rst`) still
    matches articles touching that exact path. Wildcard globs are
    deliberately skipped in slice 1; this test pins the exact-path
    case which IS supported."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "DOCS", None, files=["Documentation/foo.rst"])
        _add_patch_article(s, "doc@x", ["Documentation/foo.rst"])
        _add_patch_article(s, "elsewhere@x", ["Documentation/bar.rst"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = recent_articles_in_subsystem(s, alpha, sub)
    assert {p.message_id for p in out} == {"doc@x"}


def test_recent_articles_in_subsystem_empty_when_no_matches(seeded_db):
    with seeded_db() as s:
        sub = _add_subsystem(s, "DORMANT", None, files=["drivers/dormant/"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = recent_articles_in_subsystem(s, alpha, sub)
    assert out == []


def test_recent_articles_in_subsystem_wildcard_globs_skipped_silently(
    seeded_db,
):
    """Slice 1 doesn't index wildcard globs. A subsystem with ONLY
    wildcard rules currently returns no articles even if articles
    would conceptually match, documented behaviour, not a crash."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "ARCH-CSTAR", None, files=["arch/*/cstar/"])
        _add_patch_article(s, "match@x", ["arch/x86/cstar/init.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = recent_articles_in_subsystem(s, alpha, sub)
    assert out == []


# `active_threads_in_subsystem` integration, slice 2 of the
# per-subsystem dashboard. Same decay-weighted scoring as the
# landing-page `active_threads`, but constrained to messages
# touching the subsystem's paths.


def test_active_threads_in_subsystem_returns_threads_with_matching_path(
    seeded_db,
):
    """A thread with a recent message touching the subsystem's
    paths surfaces in the dashboard's active-threads list. A
    thread with a recent message touching unrelated paths does
    not."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(
            s, "bch@x", ["fs/bcachefs/super.c"], subject="bcachefs work"
        )
        _add_recent_thread_root(
            s, "other@x", ["fs/btrfs/extent.c"], subject="btrfs work"
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = active_threads_in_subsystem(s, alpha, sub, force=True)
    msgids = {t.message_id for t in out}
    assert "bch@x" in msgids
    assert "other@x" not in msgids


def test_active_threads_in_subsystem_respects_excludes(seeded_db):
    """X: globs veto threads whose seed message's paths are all
    excluded, same per-article-keep-if-one-in-scope rule as the
    recent-patches surface."""
    with seeded_db() as s:
        sub = _add_subsystem(
            s,
            "BTRFS-MAIN",
            "Maintained",
            files=["fs/btrfs/"],
            excludes=["fs/btrfs/tests/"],
        )
        _add_recent_thread_root(s, "main@x", ["fs/btrfs/extent.c"], subject="main work")
        _add_recent_thread_root(
            s, "tests@x", ["fs/btrfs/tests/runner.c"], subject="tests work"
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = active_threads_in_subsystem(s, alpha, sub, force=True)
    msgids = {t.message_id for t in out}
    assert msgids == {"main@x"}


def test_active_threads_in_subsystem_scoped_to_inbox(seeded_db):
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(s, "a-side@x", ["fs/bcachefs/a.c"], inbox_name="alpha")
        _add_recent_thread_root(s, "b-side@x", ["fs/bcachefs/b.c"], inbox_name="beta")
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        out_alpha = active_threads_in_subsystem(s, alpha, sub, force=True)
        out_beta = active_threads_in_subsystem(s, beta, sub, force=True)
    assert {t.message_id for t in out_alpha} == {"a-side@x"}
    assert {t.message_id for t in out_beta} == {"b-side@x"}


def test_active_threads_in_subsystem_empty_when_wildcard_only(seeded_db):
    """A subsystem with only wildcard F: rules has no supported
    globs in slice 2 and returns no active threads, same
    documented behaviour as `recent_articles_in_subsystem`."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "ARCH-CSTAR", None, files=["arch/*/cstar/"])
        _add_recent_thread_root(s, "match@x", ["arch/x86/cstar/init.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = active_threads_in_subsystem(s, alpha, sub, force=True)
    assert out == []


# `daily_volume_in_subsystem` integration.


def test_daily_volume_in_subsystem_counts_matching_articles(seeded_db):
    """An article touching a subsystem path increments that day's
    bar. An article touching an unrelated path does not."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(s, "in@x", ["fs/bcachefs/super.c"])
        _add_recent_thread_root(s, "out@x", ["fs/btrfs/extent.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        spark = daily_volume_in_subsystem(s, alpha, sub, days=7, force=True)
    # Today's bar should reflect the one in-scope article.
    today_count = sum(c for _, c in spark.days)
    assert today_count == 1


def test_daily_volume_in_subsystem_zero_fills_when_empty(seeded_db):
    """A dormant subsystem still returns a fully zero-filled
    `days` series so the sparkline renders cleanly."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "DORMANT", None, files=["drivers/dormant/"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        spark = daily_volume_in_subsystem(s, alpha, sub, days=14, force=True)
    assert len(spark.days) == 14
    assert all(c == 0 for _, c in spark.days)


def test_daily_volume_in_subsystem_returns_zero_series_for_wildcard_only(
    seeded_db,
):
    """A subsystem with only wildcard F: rules has no supported
    globs in slice 2, the sparkline still renders, all zeros."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "ARCH-CSTAR", None, files=["arch/*/cstar/"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        spark = daily_volume_in_subsystem(s, alpha, sub, days=10, force=True)
    assert len(spark.days) == 10
    assert all(c == 0 for _, c in spark.days)


def test_daily_volume_in_subsystem_returns_zero_series_for_zero_paths(
    seeded_db,
):
    """A subsystem with no path rules at all (no F:, no X:) hits the
    `inc_sql == ""` short-circuit in `_subsystem_path_filter_sql`, which
    returns None and the helper falls back to the zero-filled series
    without running an unfiltered SQL scan over `article_files`. Pin
    that here: a regression that ran the query unfiltered would either
    return non-zero counts (any article would match) or crash on an
    empty WHERE clause."""
    from mimir.subsystems_dashboard import recent_articles_in_subsystem

    with seeded_db() as s:
        sub = _add_subsystem(s, "NOPATHS", "Supported", files=[])
        # Seed an article that would match anything to prove the
        # filter is in effect: the helper must NOT pick this up.
        _add_recent_thread_root(s, "noisy@x", ["any/path/here.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        spark = daily_volume_in_subsystem(s, alpha, sub, days=5, force=True)
        recents = recent_articles_in_subsystem(s, alpha, sub, limit=10, force=True)
    assert len(spark.days) == 5
    assert all(c == 0 for _, c in spark.days)
    assert recents == []


# `active_reviewers_in_subsystem` integration, slice 2 of #97.
# The extractor itself is exercised in tests/test_trailers.py; here
# we pin the JOIN through article_files (subsystem path filter) +
# the per-reviewer aggregation contract.


# ---------------------------------------------------------------------------
# Plan-pinning: recent_articles_in_subsystem uses the IN-list shape, no SCAN
# ---------------------------------------------------------------------------
#
# CONTEXT.md 2026-05-29: PR #392 / v2.8.0 rewrote
# recent_articles_in_subsystem (and two siblings) from the IN-list shape
# `WHERE a.id IN (UNION-of-seeks)` to an EXISTS shape; the EXISTS
# regression killed lkml from 24 s to 351 s on production and was
# reverted in v2.8.1. MEMORY.md "measure perf PRs against the WORST
# inboxes": EXPLAIN-shape pins alone wouldn't have caught the v2.8.0
# regression (the EXISTS plan looked fine until production-scale data
# distribution exposed it). This test pins the COARSE shape (no full
# scan; ix_article_files_path drives) so a regression that drops the
# UNION-of-seeks index optimisation OR introduces a clearly-wrong
# SCAN would surface here.


def test_recent_articles_in_subsystem_uses_indexed_seeks_no_full_scan(seeded_db):
    """`recent_articles_in_subsystem`'s production SQL is:
    `SELECT ... FROM articles a JOIN article_lists al WHERE al.inbox_id=?
    AND a.id IN ({path_sql}) ORDER BY a.date DESC LIMIT ?`. The
    `path_sql` IN-list is the UNION-of-seeks shape from
    `_subsystem_path_filter_sql` (already plan-pinned in
    `tests/test_subsystems/test_path_filter.py`).

    Pin: no full SCAN of articles or article_lists; the path filter's
    `ix_article_files_path` index appears in the plan.
    """
    from sqlalchemy import text

    from mimir.subsystems_dashboard._path_filter import _subsystem_path_filter_sql

    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        sub = _add_subsystem(
            s,
            "PLAN-PIN-TEST",
            "Maintained",
            files=["net/", "fs/foo.c"],
            excludes=["net/bluetooth/"],
        )
        s.commit()
        path_filter = _subsystem_path_filter_sql(sub, prefix="planpin")
        assert path_filter is not None, (
            "test wiring: subsystem has includes, _subsystem_path_filter_sql "
            "should return a non-None (sql, params) tuple"
        )
        path_sql, path_params = path_filter

        full_sql = f"""
            EXPLAIN QUERY PLAN
            SELECT a.id, a.message_id, a.subject, a.author, a.date,
                   a.canonical_inbox_id
            FROM articles a
            JOIN article_lists al ON al.article_id = a.id
            WHERE al.inbox_id = :inbox_id
              AND a.id IN ({path_sql})
            ORDER BY a.date DESC
            LIMIT :limit
        """
        plan_rows = s.execute(
            text(full_sql),
            {"inbox_id": alpha.id, "limit": 20, **path_params},
        ).all()
    plan = "\n".join(r[-1] for r in plan_rows)

    assert "SCAN articles" not in plan, (
        f"recent_articles_in_subsystem must not full-scan articles; plan:\n{plan}"
    )
    assert "SCAN article_lists" not in plan, (
        f"recent_articles_in_subsystem must not full-scan article_lists; plan:\n{plan}"
    )
    assert "SCAN article_files" not in plan, (
        f"recent_articles_in_subsystem's path-filter IN-list must drive "
        f"through ix_article_files_path (the UNION-of-seeks shape), NOT "
        f"a full scan of article_files; the LIKE-with-ESCAPE shape that "
        f"disabled the index would surface here. plan:\n{plan}"
    )
    assert "ix_article_files_path" in plan, (
        f"recent_articles_in_subsystem must drive via "
        f"ix_article_files_path on the path-filter join; plan:\n{plan}"
    )
