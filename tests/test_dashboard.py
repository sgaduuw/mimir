"""Dashboard SQL helper contract.

Counts / aggregations / wildcard escaping live in
`mimir.dashboard`. The route smoke tests touch every endpoint, but
they don't sanity-check the actual numbers, these do, against the
conftest seed.

Seed recap (from conftest.py):
- alpha: art1 (2024-01-01), art3 (2024-03-01), art4 (2024-01-02)
- beta:  art2 (2024-02-01), art3 (2024-03-01)
- art3 is cross-posted between alpha and beta.
"""
from datetime import date, datetime, timedelta, timezone

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
    """`max_count` must equal the actual maximum count in the
    returned series, not just `>= 0`. Seed two new articles on the
    same recent day so the window has a non-zero peak, then assert
    max_count picks it up.

    The seeded-by-conftest articles all date to Jan 2024, which is
    outside the 30-day window for any plausible test run date, so
    without an in-window seed the helper's max_count is 0 and the
    `vol.max_count == max(...)` check passes trivially."""
    from datetime import datetime, timedelta, timezone
    from mimir.models import Article, ArticleList

    alpha = _inbox(seeded_db, "alpha")
    # Insert two articles dated 3 days ago so they fall in the
    # daily_volume(days=30) window. Same day -> count of 2 on that day,
    # 0 on the rest -> max_count == 2.
    three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)
    with seeded_db() as s:
        for i in range(2):
            art = Article(
                message_id=f"daily-volume-max-{i}@x",
                subject=f"d{i}",
                author="u@x",
                date=three_days_ago,
                thread_parent=None,
                subject_normalized=f"d{i}",
            )
            s.add(art)
            s.flush()
            s.add(ArticleList(
                article_id=art.id,
                inbox_id=alpha.id,
                epoch="0.git",
                commit_sha="ee" * 20,
            ))
        s.commit()

        vol = daily_volume(s, alpha, days=30, force=True)
    # max_count is the real maximum -- not 0, not a hardcoded constant.
    assert vol.max_count == 2
    assert vol.max_count == max(c for _, c in vol.days)
    # The two-article day shows count 2; every other day in the
    # window is 0 since seeded conftest articles are outside the
    # window.
    counts = [c for _, c in vol.days]
    assert counts.count(2) == 1
    assert counts.count(0) == len(counts) - 1


def test_dashboard_today_helpers_use_utc_not_local_date(seeded_db, monkeypatch):
    """Regression: `daily_volume` and `this_day_in_history` must derive
    'today' from `datetime.now(utc).date()`, not `date.today()`.

    `Article.date` stores public-inbox commit times in UTC (CONTEXT.md).
    `date.today()` returns the *local* date; on a non-UTC server (e.g.
    Coruscant runs CEST = UTC+2) the SQL window slides by the offset,
    slipping edge messages in/out at UTC midnight. Both cache keys also
    must advance at UTC midnight so two requests on the same UTC day
    hit the same key regardless of the server's TZ.

    The test monkey-patches `mimir.dashboard.date` so its `.today()`
    raises if called. The helpers must use `datetime.now(utc).date()`
    instead, which goes through a `datetime` instance method and does
    NOT route through the patched classmethod."""
    import mimir.dashboard as dm

    class _UTCOnlyDate(dm.date):
        @classmethod
        def today(cls):
            raise AssertionError(
                "dashboard helpers must derive 'today' from "
                "datetime.now(timezone.utc).date(), not date.today()"
            )

    monkeypatch.setattr(dm, "date", _UTCOnlyDate)

    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        # Both helpers must complete without tripping the assertion.
        daily_volume(s, alpha, days=30, force=True)
        this_day_in_history(s, alpha, years_ago=5, limit=5, force=True)


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


def test_monthly_volume_year_boundary(seeded_db):
    """An article posted on the very last second of a year must be
    attributed to that year, not the next. Off-by-one mistakes in the
    BETWEEN-year filter (e.g. inclusive vs exclusive upper bound,
    UTC drift) are an easy regression that the existing test for
    "groups_by_month" doesn't exercise."""
    from datetime import datetime, timezone
    from mimir.models import Article, ArticleList

    alpha = _inbox(seeded_db, "alpha")
    # Last hour of 2022, second-precision (no microseconds). SQLite's
    # strftime rounds .999999s up across the year boundary on some
    # builds, which is a separate edge from the BETWEEN-bounds
    # regression this test is guarding -- so stay clearly inside 2022.
    boundary = datetime(2022, 12, 31, 23, 59, 0, tzinfo=timezone.utc)
    with seeded_db() as s:
        art = Article(
            message_id="year-boundary@x",
            subject="boundary",
            author="u@x",
            date=boundary,
            thread_parent=None,
            subject_normalized="boundary",
        )
        s.add(art)
        s.flush()
        s.add(ArticleList(
            article_id=art.id, inbox_id=alpha.id,
            epoch="0.git", commit_sha="bd" * 20,
        ))
        s.commit()

        vol_2022 = monthly_volume(s, alpha, year=2022, force=True)
        vol_2023 = monthly_volume(s, alpha, year=2023, force=True)

    # Belongs to 2022, specifically December.
    assert dict(vol_2022.months)[12] >= 1, (
        f"boundary article (2022-12-31 23:59:59) missing from 2022 totals: {vol_2022}"
    )
    # Must NOT leak into the next year.
    assert all(c == 0 for _, c in vol_2023.months), (
        f"boundary article leaked into 2023: {vol_2023}"
    )


# search_articles, including the LIKE-wildcard escape


def test_search_articles_subject_substring(seeded_db):
    """Subject-substring search. The token deliberately doesn't
    coincide with the inbox name: `"alpha"` would also match the
    inbox slug, so a regression that searched the wrong column
    (or the wrong join shape) could still pass. `"hello"` appears
    only in the subject column."""
    alpha = _inbox(seeded_db, "alpha")
    ids = _ids_by_message_id(seeded_db)
    with seeded_db() as s:
        results = search_articles(s, alpha, "hello", force=True)
    # art1 ("hello alpha") and art4 ("Re: hello alpha") match;
    # art3 ("cross-posted note") doesn't.
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


def test_search_articles_cache_key_case_folds(seeded_db):
    """`Foo` and `foo` are identical to SQLite `ilike()` so they
    must collapse to one cache row. Without the case-fold in the
    cache key, each distinct casing would write its own row,
    burning cache space proportional to user-typed variation."""
    from mimir import cache
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        # First call (capitalised) writes the cache row.
        search_articles(s, alpha, "Hello", force=True)
        # Second call (lowercase) hits the row written by the first.
        search_articles(s, alpha, "hello")
    keys = [k for k in cache.keys() if k.startswith("search:alpha:")]
    assert keys == ["search:alpha:hello:100"]


def test_search_articles_no_match(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        results = search_articles(s, alpha, "wontmatch12345", force=True)
    assert results == []


def test_search_articles_inbox_scoped(seeded_db):
    """`hello` matches both art1 ("hello alpha") and art2 ("hello
    beta"), but only art1 (and friends) live in alpha."""
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
        # The seed has art1 "hello alpha", without escape, a query
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
    recent = datetime.now(timezone.utc) - timedelta(days=7)
    with seeded_db() as s:
        pull = Article(
            message_id="pull@example.com",
            subject="[GIT PULL] something", author="Maintainer",
            date=recent,
            thread_parent=None, subject_normalized="[git pull] something",
        )
        s.add(pull)
        s.flush()
        pull_id = pull.id
        s.add(ArticleList(article_id=pull_id, inbox_id=alpha.id, epoch="0.git", commit_sha="cf" * 20))
        s.commit()
        results = latest_pull_requests(s, alpha, force=True)
    assert {r.id for r in results} == {pull_id}


def test_latest_pull_requests_recency_floor_excludes_old(seeded_db):
    """A `[GIT PULL]` older than the recency floor must not appear. The
    floor exists so SQLite can stop at the floor in the reverse date-index
    scan instead of walking the full inbox proving the negative when
    fewer than `limit` matches exist."""
    from mimir.dashboard import LISTING_RECENCY_FLOOR_DAYS
    alpha = _inbox(seeded_db, "alpha")
    too_old = datetime.now(timezone.utc) - timedelta(days=LISTING_RECENCY_FLOOR_DAYS + 30)
    with seeded_db() as s:
        pull = Article(
            message_id="oldpull@example.com",
            subject="[GIT PULL] ancient", author="Maintainer",
            date=too_old,
            thread_parent=None, subject_normalized="[git pull] ancient",
        )
        s.add(pull)
        s.flush()
        s.add(ArticleList(article_id=pull.id, inbox_id=alpha.id, epoch="0.git", commit_sha="ce" * 20))
        s.commit()
        results = latest_pull_requests(s, alpha, force=True)
    assert results == []


def test_latest_stable_releases_matches_glob(seeded_db):
    """`Linux <digit>...` is the GLOB pattern."""
    alpha = _inbox(seeded_db, "alpha")
    recent = datetime.now(timezone.utc) - timedelta(days=7)
    with seeded_db() as s:
        rel = Article(
            message_id="rel@example.com",
            subject="Linux 6.10 released", author="Maintainer",
            date=recent,
            thread_parent=None, subject_normalized="linux 6.10 released",
        )
        s.add(rel)
        s.flush()
        rel_id = rel.id
        s.add(ArticleList(article_id=rel_id, inbox_id=alpha.id, epoch="0.git", commit_sha="df" * 20))
        s.commit()
        results = latest_stable_releases(s, alpha, force=True)
    assert {r.id for r in results} == {rel_id}


def test_latest_stable_releases_recency_floor_excludes_old(seeded_db):
    """Release announcements older than the recency floor must not appear."""
    from mimir.dashboard import LISTING_RECENCY_FLOOR_DAYS
    alpha = _inbox(seeded_db, "alpha")
    too_old = datetime.now(timezone.utc) - timedelta(days=LISTING_RECENCY_FLOOR_DAYS + 30)
    with seeded_db() as s:
        rel = Article(
            message_id="oldrel@example.com",
            subject="Linux 4.0 released", author="Maintainer",
            date=too_old,
            thread_parent=None, subject_normalized="linux 4.0 released",
        )
        s.add(rel)
        s.flush()
        s.add(ArticleList(article_id=rel.id, inbox_id=alpha.id, epoch="0.git", commit_sha="dd" * 20))
        s.commit()
        results = latest_stable_releases(s, alpha, force=True)
    assert results == []


# this_day_in_history


def test_this_day_in_history_returns_articles_summary(seeded_db):
    """The shape contract: returns a list of ArticleSummary. Also pin
    the date-filter behaviour: an article dated exactly `years_ago`
    ago from today's date must appear in the result.

    The older version only asserted the shape, which would have
    passed on a no-op implementation. Seed an article matching the
    today-minus-N filter and verify it's returned."""
    from datetime import datetime, timedelta, timezone
    from mimir.dashboard import ArticleSummary
    from mimir.models import Article, ArticleList

    alpha = _inbox(seeded_db, "alpha")
    years_ago = 5
    # Match the helper's date math: `now - timedelta(days=365*N)`,
    # NOT calendar-year subtraction. Leap years drift the result by
    # a couple of days vs the same calendar date, and a regression
    # that hardcoded the wrong arithmetic would silently filter
    # out articles. Compute identically to the helper.
    target = datetime.now(timezone.utc) - timedelta(days=365 * years_ago)
    target_dt = target.replace(hour=12, minute=0, second=0, microsecond=0)
    with seeded_db() as s:
        art = Article(
            message_id="history-anchor@x",
            subject="this day, five years ago",
            author="u@x",
            date=target_dt,
            thread_parent=None,
            subject_normalized="this day five years ago",
        )
        s.add(art)
        s.flush()
        s.add(ArticleList(
            article_id=art.id, inbox_id=alpha.id,
            epoch="0.git", commit_sha="hd" * 20,
        ))
        s.commit()

        results = this_day_in_history(s, alpha, years_ago=years_ago, limit=10, force=True)
    assert isinstance(results, list)
    for r in results:
        assert isinstance(r, ArticleSummary)
    # The seeded article must appear; if the date filter dropped it,
    # the helper is broken. ArticleSummary doesn't carry message_id,
    # so match on subject (unique to this test).
    assert any(r.subject == "this day, five years ago" for r in results), (
        f"this_day_in_history missed the on-target article: "
        f"target={target_dt.isoformat()}, results={[r.subject for r in results]}"
    )
    # A different article seeded out-of-window must NOT appear.
    with seeded_db() as s:
        out = Article(
            message_id="history-out-of-window@x",
            subject="out of window",
            author="u@x",
            date=target_dt + timedelta(days=7),  # 7 days off the anchor
            thread_parent=None,
            subject_normalized="out of window",
        )
        s.add(out)
        s.flush()
        s.add(ArticleList(
            article_id=out.id, inbox_id=alpha.id,
            epoch="0.git", commit_sha="hw" * 20,
        ))
        s.commit()
        results2 = this_day_in_history(s, alpha, years_ago=years_ago, limit=10, force=True)
    assert not any(r.subject == "out of window" for r in results2)


# recent_articles


def test_recent_articles_returns_summaries(seeded_db):
    from mimir.dashboard import ArticleSummary

    alpha = _inbox(seeded_db, "alpha")
    ids = _ids_by_message_id(seeded_db)
    with seeded_db() as s:
        results = recent_articles(s, alpha, limit=10, force=True)
    # alpha has art1, art3, art4, date-desc → art3 (Mar), art4 (Jan 2), art1 (Jan 1).
    assert [r.id for r in results] == [
        ids["art3@example.com"],
        ids["art4@example.com"],
        ids["art1@example.com"],
    ]
    assert all(isinstance(r, ArticleSummary) for r in results)
