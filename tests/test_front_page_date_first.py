"""Date-first shape of the inbox front-page reads (#1218).

`active_threads`, `daily_volume` and `author_recent` (the trackers) used
to start from every `article_lists` row of the inbox, 6.67M on lkml, for a
7 to 30 day window or a handful of newest rows. From
`RECENT_INBOX_FIRST_BELOW_ARTICLES` articles up they walk `ix_articles_date`
instead; a small inbox or one of unknown size keeps the inbox walk.

Plan tests pin `PRODUCTION_STATS` on purpose: the date-first statements pin
their shape with `INDEXED BY`, and the inbox-first ones rely on production's
statistics to pick the inbox index, so a pin of EMPTY would not show the
plan production gets (a PRODUCTION_STATS pin once hid a regression, #798;
here it is the plan we want to see, and the result tests need no stats).
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from mimir import cache
from mimir.dashboard import (
    RECENT_INBOX_FIRST_BELOW_ARTICLES,
    _archive_stats_key,
    _listing_rows,
    author_recent,
    daily_volume,
)
from mimir.models import Article, ArticleList, Inbox
from mimir.threading import active_threads
from tests._helpers import PRODUCTION_STATS, _cache_total, plan_of, planner_stats

BIG = RECENT_INBOX_FIRST_BELOW_ARTICLES
SMALL = RECENT_INBOX_FIRST_BELOW_ARTICLES - 1
UNKNOWN = None


def _alpha(s):
    return s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()


def _now():
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def _add(s, inbox, msgid, when, author="trk@example.com"):
    art = Article(
        message_id=msgid,
        subject=msgid,
        author=author,
        date=when,
        thread_parent=None,
        subject_normalized=msgid,
        lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="f" * 40)],
    )
    s.add(art)
    s.flush()
    return art.id


def _size(inbox, total):
    if total is UNKNOWN:
        cache.delete(_archive_stats_key(inbox))
    else:
        _cache_total(inbox, total)


# (call, a fragment unique to the statement it runs)
def _threads(s, inbox, **kw):
    return active_threads(s, inbox, force=True, **kw)


def _volume(s, inbox, **kw):
    return daily_volume(s, inbox, force=True, **kw)


def _trackers(s, inbox, **kw):
    # limit 1 and one recent row seeded, so the date walk satisfies it
    # and the fallback statement never runs.
    return author_recent(s, inbox, "trk@", limit=1, force=True)


READS = [
    pytest.param(_threads, "WITH RECURSIVE chains", id="active_threads"),
    pytest.param(_volume, "date(a.date) AS day", id="daily_volume"),
    pytest.param(_trackers, "a.author LIKE", id="author_recent"),
]


@pytest.mark.parametrize("total", [BIG, BIG * 66])
@pytest.mark.parametrize(("call", "match"), READS)
def test_large_inbox_walks_the_date_index(seeded_db, call, match, total):
    with seeded_db() as s:
        _add(s, _alpha(s), "recent@x", _now() - timedelta(days=1))
        s.commit()
    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        alpha = _alpha(s)
        _size(alpha, total)
        plan = plan_of(s, lambda: call(s, alpha), match)
    lines = plan.splitlines()
    assert any(
        ln.startswith("SEARCH a ") and "INDEX ix_articles_date" in ln for ln in lines
    ), plan
    assert "ix_article_lists_inbox_id" not in plan, plan


@pytest.mark.parametrize("total", [SMALL, 1, UNKNOWN])
@pytest.mark.parametrize(("call", "match"), READS)
def test_small_or_unknown_inbox_keeps_the_inbox_walk(seeded_db, call, match, total):
    with seeded_db() as s:
        _add(s, _alpha(s), "recent@x", _now() - timedelta(days=1))
        s.commit()
    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        alpha = _alpha(s)
        _size(alpha, total)
        plan = plan_of(s, lambda: call(s, alpha), match)
    assert "ix_articles_date" not in plan, plan
    assert "ix_article_lists_inbox_id" in plan, plan


@pytest.mark.parametrize(
    "fn",
    [_threads, _volume],
    ids=["active_threads", "daily_volume"],
)
def test_both_shapes_return_the_same_window(seeded_db, fn):
    """Same rows either way: in-window rows of this inbox only, a crosspost
    counted once, the old row and beta's row left out."""
    now = _now()
    with seeded_db() as s:
        alpha = _alpha(s)
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        _add(s, alpha, "in1@x", now - timedelta(days=1))
        _add(s, alpha, "in2@x", now - timedelta(days=2))
        _add(s, alpha, "old@x", now - timedelta(days=200))
        _add(s, beta, "beta@x", now - timedelta(days=1))
        s.commit()
        out = {}
        for key, total in (("inbox", SMALL), ("date", BIG)):
            _size(alpha, total)
            res = fn(s, alpha)
            out[key] = [t.message_id for t in res] if fn is _threads else res.days[-3:]
    assert out["inbox"] == out["date"]
    if fn is _threads:
        assert sorted(out["date"]) == ["in1@x", "in2@x"]
    else:
        assert sum(n for _, n in out["date"]) == 2


# author_recent: the date walk must return what the inbox scan returns.

_LIMIT = 3
# Days before now for each tracked row (None: undated); "other" rows are
# not the tracker's and "later" rows sit in a second inbox.
TRACKER_CASES = {
    # more than `limit` inside the floor: the walk alone answers
    "all_inside": [1, 5, 9, 20, 40, 400],
    # exactly `limit` inside, older ones beyond it
    "exactly_limit_inside": [1, 5, 9, 400, 500],
    # some inside the floor, the rest only found by the inbox scan
    "straddling": [1, 400, 500, 600],
    # nothing inside the floor; an undated row sorts last
    "none_inside": [400, 500, 600, None],
    "only_undated": [None, None],
    "no_rows": [],
}


def _seed_tracker(s, inbox, days):
    now = _now()
    return [
        _add(s, inbox, f"t{i}@x", None if d is None else now - timedelta(days=d))
        for i, d in enumerate(days)
    ]


def _oracle(s, inbox, limit):
    """Today's statement: inbox-first, no floor."""
    return _listing_rows(
        s, inbox, "a.author LIKE :pattern", {"pattern": "%trk@%"}, limit
    )


@pytest.mark.parametrize("days", TRACKER_CASES.values(), ids=TRACKER_CASES.keys())
@pytest.mark.parametrize("total", [BIG, SMALL], ids=["date_first", "inbox_first"])
def test_author_recent_matches_the_inbox_scan(seeded_db, days, total):
    with seeded_db() as s:
        alpha = _alpha(s)
        _seed_tracker(s, alpha, days)
        # noise that must never appear: another author, another inbox
        _add(s, alpha, "noise@x", _now() - timedelta(days=1), author="x@else.org")
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        _add(s, beta, "beta@x", _now() - timedelta(days=1))
        s.commit()
        _size(alpha, total)
        got = author_recent(s, alpha, "trk@", limit=_LIMIT, force=True)
        want = _oracle(s, alpha, _LIMIT)
    assert got == want
    assert len(got) == min(_LIMIT, len(days))


def test_author_recent_straddling_reaches_past_the_floor(seeded_db):
    """Not vacuous: one row inside, two more only from beyond the floor."""
    with seeded_db() as s:
        alpha = _alpha(s)
        ids = _seed_tracker(s, alpha, [1, 400, 500, 600])
        s.commit()
        _size(alpha, BIG)
        got = author_recent(s, alpha, "trk@", limit=_LIMIT, force=True)
    assert [r.id for r in got] == ids[:3]


def test_author_recent_date_ties_at_the_limit_keep_the_highest_ids(seeded_db):
    """Four rows share one date inside the floor, limit 3: the newest
    three ids, highest first, as the inbox scan orders them."""
    when = _now() - timedelta(days=2)
    with seeded_db() as s:
        alpha = _alpha(s)
        ids = [_add(s, alpha, f"tie{i}@x", when) for i in range(4)]
        _add(s, alpha, "older@x", _now() - timedelta(days=3))
        s.commit()
        _size(alpha, BIG)
        got = author_recent(s, alpha, "trk@", limit=_LIMIT, force=True)
        want = _oracle(s, alpha, _LIMIT)
    assert [r.id for r in got] == ids[::-1][:3]
    assert got == want


def test_author_recent_ties_across_the_fallback(seeded_db):
    """One row inside the floor, then a date tie beyond it: the fallback
    keeps the tie's highest id, as the inbox scan does."""
    old = _now() - timedelta(days=400)
    with seeded_db() as s:
        alpha = _alpha(s)
        recent = _add(s, alpha, "recent@x", _now() - timedelta(days=1))
        tie = [_add(s, alpha, f"tie{i}@x", old) for i in range(3)]
        s.commit()
        _size(alpha, BIG)
        got = author_recent(s, alpha, "trk@", limit=2, force=True)
    assert [r.id for r in got] == [recent, tie[-1]]
