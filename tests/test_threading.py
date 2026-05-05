"""Recursive-CTE thread-reconstruction contract.

Bugs in `mimir.threading`'s CTEs surface as wrong threads displayed,
and they're hard to spot manually because the corpus drowns the
edge cases. These tests build small explicit thread shapes against
the seeded test DB and pin the walk semantics.

The conftest seed already gives us:
- art1: alpha only, root
- art4: alpha only, replies to art1 (small thread)
- art2: beta only, standalone
- art3: alpha + beta, standalone

Tests that need richer shapes add extra Articles inline.
"""
from datetime import datetime, timezone

from sqlalchemy import select

from mimir.models import Article, ArticleList, Inbox
from mimir.threading import (
    MAX_DEPTH,
    _active_threads_query,
    find_thread_root,
    get_thread,
    threads_for_day,
    threads_for_month,
)


def _inbox(seeded_db, name: str) -> Inbox:
    """Helper: detached Inbox for the given name. Threading helpers
    only read .id and .name; no need for session attachment."""
    with seeded_db() as s:
        return s.execute(select(Inbox).where(Inbox.name == name)).scalar_one()


# find_thread_root


def test_find_thread_root_walks_up_to_root(seeded_db):
    """art4 → art1 → (no parent in DB). Root from art4 is art1."""
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        root = find_thread_root(s, alpha, "art4@example.com")
    assert root == "art1@example.com"


def test_find_thread_root_returns_self_when_root(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        root = find_thread_root(s, alpha, "art1@example.com")
    assert root == "art1@example.com"


def test_find_thread_root_scoped_to_inbox(seeded_db):
    """art4 is in alpha but not beta. Walking from art4 against beta
    finds nothing."""
    beta = _inbox(seeded_db, "beta")
    with seeded_db() as s:
        root = find_thread_root(s, beta, "art4@example.com")
    assert root is None


def test_find_thread_root_returns_none_for_unknown(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        root = find_thread_root(s, alpha, "nonexistent@example.com")
    assert root is None


def test_find_thread_root_off_list_parent_stops_walk(seeded_db):
    """If thread_parent points at a Message-ID we don't have in the
    inbox, the walk-up halts at the deepest in-inbox ancestor."""
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        # art_orphan replies to "off-list@x.com" which isn't in DB.
        orphan = Article(
            message_id="orphan@example.com", subject="orphan",
            author="X", date=datetime(2024, 4, 1, tzinfo=timezone.utc),
            thread_parent="off-list@x.com", subject_normalized="orphan",
        )
        s.add(orphan)
        s.flush()
        s.add(ArticleList(article_id=orphan.id, inbox_id=alpha.id, epoch="0.git", commit_sha="ee" * 20))
        s.commit()
        root = find_thread_root(s, alpha, "orphan@example.com")
    # off-list parent → orphan is its own root in our walk.
    assert root == "orphan@example.com"


# get_thread


def test_get_thread_returns_root_alone_when_no_replies(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        thread = get_thread(s, alpha, "art3@example.com")
    assert len(thread) == 1
    assert thread[0].message_id == "art3@example.com"
    assert thread[0].depth == 0


def test_get_thread_walks_down_to_replies(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        thread = get_thread(s, alpha, "art1@example.com")
    msgids = [n.message_id for n in thread]
    assert msgids == ["art1@example.com", "art4@example.com"]
    depths = [n.depth for n in thread]
    assert depths == [0, 1]


def test_get_thread_scoped_to_inbox(seeded_db):
    """art1 + art4 are both alpha-only. Walking from beta returns
    nothing (no rows match)."""
    beta = _inbox(seeded_db, "beta")
    with seeded_db() as s:
        thread = get_thread(s, beta, "art1@example.com")
    assert thread == []


def test_get_thread_orders_siblings_by_date(seeded_db):
    """Add two replies to art3; depth-first by date should yield
    them in insertion-date order."""
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        reply_early = Article(
            message_id="r1@example.com", subject="Re: cross-posted note",
            author="E", date=datetime(2024, 3, 2, tzinfo=timezone.utc),
            thread_parent="art3@example.com",
            subject_normalized="cross-posted note",
        )
        reply_late = Article(
            message_id="r2@example.com", subject="Re: cross-posted note",
            author="L", date=datetime(2024, 3, 3, tzinfo=timezone.utc),
            thread_parent="art3@example.com",
            subject_normalized="cross-posted note",
        )
        s.add_all([reply_early, reply_late])
        s.flush()
        s.add_all([
            ArticleList(article_id=reply_early.id, inbox_id=alpha.id, epoch="0.git", commit_sha="11" * 20),
            ArticleList(article_id=reply_late.id, inbox_id=alpha.id, epoch="0.git", commit_sha="12" * 20),
        ])
        s.commit()
        thread = get_thread(s, alpha, "art3@example.com")

    msgids = [n.message_id for n in thread]
    assert msgids == ["art3@example.com", "r1@example.com", "r2@example.com"]


def test_get_thread_depth_for_nested_replies(seeded_db):
    """Build a 3-deep chain and verify depth values."""
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        depth2 = Article(
            message_id="d2@example.com", subject="Re: hello alpha",
            author="X", date=datetime(2024, 1, 3, tzinfo=timezone.utc),
            thread_parent="art4@example.com",
            subject_normalized="hello alpha",
        )
        depth3 = Article(
            message_id="d3@example.com", subject="Re: hello alpha",
            author="Y", date=datetime(2024, 1, 4, tzinfo=timezone.utc),
            thread_parent="d2@example.com",
            subject_normalized="hello alpha",
        )
        s.add_all([depth2, depth3])
        s.flush()
        s.add_all([
            ArticleList(article_id=depth2.id, inbox_id=alpha.id, epoch="0.git", commit_sha="21" * 20),
            ArticleList(article_id=depth3.id, inbox_id=alpha.id, epoch="0.git", commit_sha="22" * 20),
        ])
        s.commit()
        thread = get_thread(s, alpha, "art1@example.com")

    by_id = {n.message_id: n.depth for n in thread}
    assert by_id["art1@example.com"] == 0
    assert by_id["art4@example.com"] == 1
    assert by_id["d2@example.com"] == 2
    assert by_id["d3@example.com"] == 3


def test_get_thread_max_depth_caps_runaway():
    """The recursive-CTE depth cap is configured at MAX_DEPTH; pin
    the value so a future change doesn't accidentally raise it
    enough to enable runaway loops on a synthetic cycle."""
    assert MAX_DEPTH == 1000


# active_threads / threads_for_day / threads_for_month


def test_active_threads_query_returns_root_for_recent_reply(seeded_db):
    """Window covering 2024-01-02 should pick up art1's thread (its
    reply art4 is in the window)."""
    alpha = _inbox(seeded_db, "alpha")
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, tzinfo=timezone.utc)
    with seeded_db() as s:
        results = _active_threads_query(s, alpha, start, end, order_by="last_activity", limit=None)
    msgids = {r.message_id for r in results}
    # Both art1 and art4 are in the window; both walk up to art1 as
    # root, so the deduped result should contain art1 once.
    assert "art1@example.com" in msgids


def test_threads_for_day_returns_only_that_days_threads(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        results = threads_for_day(s, alpha, datetime(2024, 1, 1).date(), force=True)
    msgids = {r.message_id for r in results}
    # art1 was on 2024-01-01.
    assert "art1@example.com" in msgids
    # art2 was on 2024-02-01, beta only — must not appear.
    assert "art2@example.com" not in msgids


def test_threads_for_day_empty_day(seeded_db):
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        results = threads_for_day(s, alpha, datetime(2030, 1, 1).date(), force=True)
    assert results == []


def test_threads_for_month_groups_by_month(seeded_db):
    """art1 (2024-01-01) and art4 (2024-01-02) are both in alpha.
    Threading collapses them into one root."""
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        results = threads_for_month(s, alpha, 2024, 1, force=True)
    msgids = {r.message_id for r in results}
    assert msgids == {"art1@example.com"}


def test_threads_for_month_excludes_other_inbox(seeded_db):
    """art1 is alpha-only. Querying beta for the same month
    should not return it."""
    beta = _inbox(seeded_db, "beta")
    with seeded_db() as s:
        results = threads_for_month(s, beta, 2024, 1, force=True)
    msgids = {r.message_id for r in results}
    assert "art1@example.com" not in msgids


def test_threads_for_month_respects_limit(seeded_db):
    """Seed five extra threads in March, then cap the result at 2."""
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        for i in range(5):
            art = Article(
                message_id=f"extra{i}@example.com",
                subject=f"thread {i}",
                author="X",
                date=datetime(2024, 5, 1, 12, i, tzinfo=timezone.utc),
                thread_parent=None, subject_normalized=f"thread {i}",
            )
            s.add(art)
            s.flush()
            s.add(ArticleList(article_id=art.id, inbox_id=alpha.id, epoch="0.git", commit_sha=f"{i:02d}" * 20))
        s.commit()
        results = threads_for_month(s, alpha, 2024, 5, limit=2, force=True)
    assert len(results) == 2
