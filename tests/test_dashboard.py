"""Dashboard SQL helper contract.

Counts / aggregations / wildcard escaping live in
`mimir.dashboard`. The route smoke tests touch every endpoint, but
they don't sanity-check the actual numbers — these do, against the
conftest seed.

Seed recap (from conftest.py):
- alpha: art1 (2024-01-01), art3 (2024-03-01), art4 (2024-01-02)
- beta:  art2 (2024-02-01), art3 (2024-03-01)
- art3 is cross-posted between alpha and beta.
"""
from datetime import date, datetime, timezone

from sqlalchemy import select

from mimir.dashboard import (
    archive_stats,
    author_recent,
    daily_volume,
    latest_pull_requests,
    latest_stable_releases,
    monthly_volume,
    recent_articles,
    search_articles,
    this_day_in_history,
)
from mimir.models import Article, ArticleList, Inbox


def _inbox(seeded_db, name: str) -> Inbox:
    with seeded_db() as s:
        return s.execute(select(Inbox).where(Inbox.name == name)).scalar_one()


def _ids_by_message_id(seeded_db) -> dict[str, int]:
    """ArticleSummary carries `.id` (int), not `.message_id`. Tests
    look up the expected ints from the seed via this helper."""
    with seeded_db() as s:
        return {
            mid: aid for mid, aid in s.execute(
                select(Article.message_id, Article.id)
            )
        }


# archive_stats


def test_archive_stats_total_count(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        stats = archive_stats(s, alpha, force=True)
    # alpha has art1, art3, art4 → 3
    assert stats.total == 3


def test_archive_stats_first_and_last_date(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        stats = archive_stats(s, alpha, force=True)
    # SQLite stores DATETIME as text without tz; the helper coerces
    # back via fromisoformat. Compare on (year, month, day, hour).
    assert stats.first_date is not None and stats.last_date is not None
    assert (stats.first_date.year, stats.first_date.month, stats.first_date.day) == (2024, 1, 1)
    assert (stats.last_date.year, stats.last_date.month, stats.last_date.day) == (2024, 3, 1)


def test_archive_stats_respects_min_plausible_date(seeded_db):
    """A backdated message before STATS_MIN_PLAUSIBLE_DATE (1995-01-01)
    must NOT pull `first_date` before that floor."""
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        backdated = Article(
            message_id="ancient@example.com", subject="hello",
            author="X",
            # 1991, before lkml itself existed.
            date=datetime(1991, 1, 1, tzinfo=timezone.utc),
            thread_parent=None, subject_normalized="hello",
        )
        s.add(backdated)
        s.flush()
        s.add(ArticleList(article_id=backdated.id, inbox_id=alpha.id, epoch="0.git", commit_sha="ff" * 20))
        s.commit()
        stats = archive_stats(s, alpha, force=True)
    # Total count includes the row; first_date does NOT pull back
    # before the floor.
    assert stats.total == 4
    assert stats.first_date.year >= 1995


def test_archive_stats_epoch_count(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        stats = archive_stats(s, alpha, force=True)
    assert stats.epochs == 1


def test_archive_stats_inbox_isolated(seeded_db):
    """Stats for alpha don't include beta-only rows (or vice-versa)."""
    alpha = _inbox(seeded_db, "alpha")
    beta = _inbox(seeded_db, "beta")
    with seeded_db() as s:
        a_stats = archive_stats(s, alpha, force=True)
        b_stats = archive_stats(s, beta, force=True)
    assert a_stats.total == 3
    assert b_stats.total == 2


# daily_volume / monthly_volume


def test_daily_volume_zero_fills_missing_days(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        vol = daily_volume(s, alpha, days=30, force=True)
    assert len(vol.days) == 30
    # Every day a (date, int) tuple.
    for d, count in vol.days:
        assert isinstance(d, date)
        assert isinstance(count, int)


def test_daily_volume_max_count(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        vol = daily_volume(s, alpha, days=30, force=True)
    # Seeded alpha articles are all from 2024 (outside the recent
    # 30-day window when the test runs after that), so all per-day
    # counts in the window are 0 → max_count == 0. The helper's
    # `default=1` only kicks in if the series is empty, which it
    # never is for days >= 1.
    assert vol.max_count == max(c for _, c in vol.days)
    assert vol.max_count >= 0


def test_monthly_volume_groups_by_month(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        vol = monthly_volume(s, alpha, year=2024, force=True)
    # 12 cells, every month present.
    months = dict(vol.months)
    assert sorted(months.keys()) == list(range(1, 13))
    # alpha has art1 (Jan), art4 (Jan), art3 (Mar)
    assert months[1] == 2
    assert months[2] == 0
    assert months[3] == 1
    assert vol.year == 2024
    assert vol.total == 3


def test_monthly_volume_other_year_empty(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        vol = monthly_volume(s, alpha, year=2030, force=True)
    assert vol.total == 0
    assert all(count == 0 for _, count in vol.months)


# search_articles — including the LIKE-wildcard escape


def test_search_articles_subject_substring(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    ids = _ids_by_message_id(seeded_db)
    with seeded_db() as s:
        results = search_articles(s, alpha, "alpha", force=True)
    # art1 ("hello alpha") and art4 ("Re: hello alpha") match.
    assert {r.id for r in results} == {ids["art1@example.com"], ids["art4@example.com"]}


def test_search_articles_author_substring(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    ids = _ids_by_message_id(seeded_db)
    with seeded_db() as s:
        results = search_articles(s, alpha, "carol@", force=True)
    # Carol is the author of art3 (cross-posted to alpha).
    assert {r.id for r in results} == {ids["art3@example.com"]}


def test_search_articles_case_insensitive(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        upper = search_articles(s, alpha, "ALPHA", force=True)
        lower = search_articles(s, alpha, "alpha", force=True)
    assert {r.id for r in upper} == {r.id for r in lower}


def test_search_articles_no_match(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        results = search_articles(s, alpha, "wontmatch12345", force=True)
    assert results == []


def test_search_articles_inbox_scoped(seeded_db):
    """`hello` matches both art1 ("hello alpha") and art2 ("hello
    beta") — but only art1 (and friends) live in alpha."""
    alpha = _inbox(seeded_db, "alpha")
    ids = _ids_by_message_id(seeded_db)
    with seeded_db() as s:
        results = search_articles(s, alpha, "hello", force=True)
    result_ids = {r.id for r in results}
    assert ids["art2@example.com"] not in result_ids
    assert ids["art1@example.com"] in result_ids


def test_search_articles_escapes_percent_wildcard(seeded_db):
    """A query containing `%` must be treated as a literal substring,
    not a SQL LIKE wildcard. With `100%` we get rows literally
    containing "100%", not every row in the inbox."""
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        # Seed an article whose subject contains a literal '%'.
        art_pct = Article(
            message_id="pct@example.com", subject="100% complete",
            author="X", date=datetime(2024, 4, 1, tzinfo=timezone.utc),
            thread_parent=None, subject_normalized="100% complete",
        )
        s.add(art_pct)
        s.flush()
        pct_id = art_pct.id
        s.add(ArticleList(article_id=pct_id, inbox_id=alpha.id, epoch="0.git", commit_sha="bf" * 20))
        s.commit()

        # Query for "100%" as substring; with proper escape, only
        # the seeded row matches.
        results = search_articles(s, alpha, "100%", force=True)

    assert {r.id for r in results} == {pct_id}


def test_search_articles_escapes_underscore_wildcard(seeded_db):
    """`_` in LIKE matches any single char; we escape it so a query
    with underscore matches only literal underscores."""
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        # The seed has art1 "hello alpha" — without escape, a query
        # of "h_llo" would match. With escape it must not.
        results = search_articles(s, alpha, "h_llo", force=True)
    assert results == []


def test_search_articles_respects_limit(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        results = search_articles(s, alpha, "hello", limit=1, force=True)
    assert len(results) == 1


# author_recent / latest_pull_requests / latest_stable_releases


def test_author_recent_orders_date_desc(seeded_db):
    """alpha has Alice (art1, Jan 1), Carol (art3, Mar 1), Dave (art4,
    Jan 2). Asking for the @example.com substring matches Alice and
    Dave; Carol's @kernel.org doesn't."""
    alpha = _inbox(seeded_db, "alpha")
    ids = _ids_by_message_id(seeded_db)
    with seeded_db() as s:
        results = author_recent(s, alpha, "@example.com", limit=5, force=True)
    # Newest first: art4 (Jan 2) before art1 (Jan 1).
    assert [r.id for r in results] == [ids["art4@example.com"], ids["art1@example.com"]]


def test_author_recent_respects_limit(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        results = author_recent(s, alpha, "@example.com", limit=1, force=True)
    assert len(results) == 1


def test_latest_pull_requests_matches_subject_prefix(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        pull = Article(
            message_id="pull@example.com",
            subject="[GIT PULL] something", author="Maintainer",
            date=datetime(2024, 6, 1, tzinfo=timezone.utc),
            thread_parent=None, subject_normalized="[git pull] something",
        )
        s.add(pull)
        s.flush()
        pull_id = pull.id
        s.add(ArticleList(article_id=pull_id, inbox_id=alpha.id, epoch="0.git", commit_sha="cf" * 20))
        s.commit()
        results = latest_pull_requests(s, alpha, force=True)
    assert {r.id for r in results} == {pull_id}


def test_latest_stable_releases_matches_glob(seeded_db):
    """`Linux <digit>...` is the GLOB pattern."""
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        rel = Article(
            message_id="rel@example.com",
            subject="Linux 6.10 released", author="Maintainer",
            date=datetime(2024, 7, 1, tzinfo=timezone.utc),
            thread_parent=None, subject_normalized="linux 6.10 released",
        )
        s.add(rel)
        s.flush()
        rel_id = rel.id
        s.add(ArticleList(article_id=rel_id, inbox_id=alpha.id, epoch="0.git", commit_sha="df" * 20))
        s.commit()
        results = latest_stable_releases(s, alpha, force=True)
    assert {r.id for r in results} == {rel_id}


# this_day_in_history


def test_this_day_in_history_returns_articles_summary(seeded_db):
    """Just exercise the shape — the date-based filter is hard to
    pin without freezing the clock; we just confirm it returns a
    list of ArticleSummary and doesn't raise."""
    from mimir.dashboard import ArticleSummary

    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        results = this_day_in_history(s, alpha, years_ago=5, limit=3, force=True)
    assert isinstance(results, list)
    for r in results:
        assert isinstance(r, ArticleSummary)


# recent_articles


def test_recent_articles_returns_summaries(seeded_db):
    from mimir.dashboard import ArticleSummary

    alpha = _inbox(seeded_db, "alpha")
    ids = _ids_by_message_id(seeded_db)
    with seeded_db() as s:
        results = recent_articles(s, alpha, limit=10, force=True)
    # alpha has art1, art3, art4 — date-desc → art3 (Mar), art4 (Jan 2), art1 (Jan 1).
    assert [r.id for r in results] == [
        ids["art3@example.com"],
        ids["art4@example.com"],
        ids["art1@example.com"],
    ]
    assert all(isinstance(r, ArticleSummary) for r in results)
