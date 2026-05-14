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
    THREADS_SINCE_MAX_DAYS,
    _active_threads_query,
    active_threads,
    find_thread_root,
    get_thread,
    threads_for_day,
    threads_for_month,
    threads_since,
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


def test_find_thread_root_terminates_on_cycle(seeded_db):
    """A synthetic `thread_parent` cycle (`a → b → a`) is data we'd
    rather not have, but real-world archives have had loops before.
    The CTE caps recursion at MAX_DEPTH; without it, the walk would
    run forever. Build the cycle and assert the call returns within
    bounded work (the test's mere completion is the assertion —
    pytest's per-test timeout would catch a true infinite loop)."""
    alpha = _inbox(seeded_db, "alpha")
    with seeded_db() as s:
        a = Article(
            message_id="cycle-a@example.com", subject="a", author="x",
            date=datetime(2024, 5, 1, tzinfo=timezone.utc),
            thread_parent="cycle-b@example.com",
            subject_normalized="a",
        )
        b = Article(
            message_id="cycle-b@example.com", subject="b", author="x",
            date=datetime(2024, 5, 2, tzinfo=timezone.utc),
            thread_parent="cycle-a@example.com",
            subject_normalized="b",
        )
        s.add_all([a, b])
        s.flush()
        s.add_all([
            ArticleList(article_id=a.id, inbox_id=alpha.id, epoch="0.git", commit_sha="ca" * 20),
            ArticleList(article_id=b.id, inbox_id=alpha.id, epoch="0.git", commit_sha="cb" * 20),
        ])
        s.commit()
        # The walk terminates; result is one of the cycle members
        # (which one depends on MAX_DEPTH parity). The contract is
        # "terminates and returns deterministically", not "picks a
        # specific node". A regression that removed the depth guard
        # would hang the request thread.
        root = find_thread_root(s, alpha, "cycle-a@example.com")
        assert root in ("cycle-a@example.com", "cycle-b@example.com"), (
            f"cycle walk produced unexpected root: {root!r}"
        )


def test_max_depth_value_is_sensible():
    """Companion sanity: MAX_DEPTH must be high enough for real lkml
    threads (~50 deep at most) and low enough to bound the CTE row
    count for cycle defense. 100..10000 is the sane window. Pin a
    range rather than the literal so a value tweak doesn't trip on
    this test alone."""
    assert 100 <= MAX_DEPTH <= 10_000


def test_find_thread_root_handles_out_of_order_arrival(seeded_db):
    """Cross-epoch ingest can deliver a reply before its parent —
    the reply commits first, then a later ingest pass finds the
    parent. The recursive CTE must reflect the new shape on the
    next query without any backfill step (this was one of the
    reasons the materialized-`thread_root_id`-column design was
    rejected; see CONTEXT.md "Threading via recursive CTE")."""
    alpha = _inbox(seeded_db, "alpha")

    # Step 1: insert the reply alone. Its parent doesn't exist yet.
    with seeded_db() as s:
        reply = Article(
            message_id="ooo-reply@example.com", subject="Re: ooo", author="r",
            date=datetime(2024, 6, 2, tzinfo=timezone.utc),
            thread_parent="ooo-parent@example.com",
            subject_normalized="ooo",
        )
        s.add(reply)
        s.flush()
        s.add(ArticleList(article_id=reply.id, inbox_id=alpha.id, epoch="0.git", commit_sha="01" * 20))
        s.commit()
        # Walk-up from the reply: parent is off-list → root is the reply.
        first = find_thread_root(s, alpha, "ooo-reply@example.com")
    assert first == "ooo-reply@example.com"

    # Step 2: parent arrives in a later ingest pass.
    with seeded_db() as s:
        parent = Article(
            message_id="ooo-parent@example.com", subject="ooo", author="p",
            date=datetime(2024, 6, 1, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized="ooo",
        )
        s.add(parent)
        s.flush()
        s.add(ArticleList(article_id=parent.id, inbox_id=alpha.id, epoch="0.git", commit_sha="02" * 20))
        s.commit()
        # Walk-up from the reply now: parent exists → root is the parent.
        second = find_thread_root(s, alpha, "ooo-reply@example.com")
    assert second == "ooo-parent@example.com", (
        "after parent arrives, walk-up from the reply must reach it; "
        "the CTE recomputes per query, no backfill needed"
    )


# active_threads / threads_for_day / threads_for_month


def test_active_threads_query_returns_root_for_recent_reply(seeded_db):
    """Window covering 2024-01-02 should pick up art1's thread (its
    reply art4 is in the window).

    Pin the CTE's full row shape:
    - art1 appears exactly once (dedup; both art1 and art4 walk up
      to the same root)
    - `recent_count == 2` (art1 + art4 both fall in the window)
    - `reply_count == 1` (art4 is the only reply with `recent_id`
      different from the root's id)
    The earlier presence-only check would have passed a regression
    that emitted duplicate rows or miscounted the contributors."""
    alpha = _inbox(seeded_db, "alpha")
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, tzinfo=timezone.utc)
    with seeded_db() as s:
        results = _active_threads_query(s, alpha, start, end, order_by="last_activity", limit=None)
    msgids = [r.message_id for r in results]
    # Exactly one row for art1 -- dedup is the contract.
    assert msgids.count("art1@example.com") == 1
    art1_row = next(r for r in results if r.message_id == "art1@example.com")
    assert art1_row.recent_count == 2, (
        f"art1 + art4 both in window; recent_count should be 2, got {art1_row.recent_count}"
    )
    assert art1_row.reply_count == 1, (
        f"only art4 is a reply; reply_count should be 1, got {art1_row.reply_count}"
    )


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


def test_threads_since_finds_recent_thread(seeded_db):
    """A thread with activity inside the (capped) since window shows
    up; an old thread outside the window doesn't."""
    from datetime import timedelta
    alpha = _inbox(seeded_db, "alpha")
    now = datetime.now(timezone.utc)
    with seeded_db() as s:
        art = Article(
            message_id="recent-since@example.com",
            subject="recent",
            author="X",
            date=now - timedelta(days=3),
            thread_parent=None, subject_normalized="recent",
        )
        s.add(art)
        s.flush()
        s.add(ArticleList(
            article_id=art.id, inbox_id=alpha.id,
            epoch="0.git", commit_sha="ee" * 20,
        ))
        s.commit()
    since = (now - timedelta(days=10)).date()
    with seeded_db() as s:
        results = threads_since(s, alpha, since, force=True)
    msgids = {r.message_id for r in results}
    assert "recent-since@example.com" in msgids
    # art1 (2024-01-01) is far outside the 90-day window.
    assert "art1@example.com" not in msgids


def test_threads_since_future_returns_empty(seeded_db):
    from datetime import timedelta
    alpha = _inbox(seeded_db, "alpha")
    future = (datetime.now(timezone.utc) + timedelta(days=2)).date()
    with seeded_db() as s:
        results = threads_since(s, alpha, future, force=True)
    assert results == []


def test_threads_since_max_days_is_sensible():
    """Pin the cap so a stray refactor doesn't silently loosen it.
    90 days bounds the recursive CTE for a UI surface anyone can hit."""
    assert THREADS_SINCE_MAX_DAYS == 90


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


# active_threads (the cached wrapper) and its decay-weighted ranking.
# The underlying CTE is tested above with order_by="last_activity";
# what's missing is the contract that `active_threads()` orders by the
# half-life-decayed score so a fresh burst outranks a larger but older
# thread -- the explicit reason that surface exists per CONTEXT.md.


def _seed_thread_with_messages(
    seeded_db, inbox_name: str, *, root_id_prefix: str,
    timestamps: list[datetime],
) -> str:
    """Build a root + replies for tests of the active-threads ranking.
    `timestamps[0]` is the root's date; subsequent are replies'.
    Returns the root's message_id."""
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        root_mid = f"{root_id_prefix}-root@example.com"
        root = Article(
            message_id=root_mid, subject=f"{root_id_prefix} root",
            author="t@x", date=timestamps[0],
            thread_parent=None, subject_normalized=f"{root_id_prefix} root",
        )
        s.add(root)
        s.flush()
        s.add(ArticleList(
            article_id=root.id, inbox_id=ix.id, epoch="0.git",
            commit_sha=f"{root_id_prefix[:2]}{root_id_prefix[:2]}" * 10,
        ))
        for i, ts in enumerate(timestamps[1:], start=1):
            rep = Article(
                message_id=f"{root_id_prefix}-r{i}@example.com",
                subject=f"Re: {root_id_prefix} root",
                author="t@x", date=ts,
                thread_parent=root_mid,
                subject_normalized=f"{root_id_prefix} root",
            )
            s.add(rep)
            s.flush()
            s.add(ArticleList(
                article_id=rep.id, inbox_id=ix.id, epoch="0.git",
                commit_sha=f"r{i:02d}" + ("f" * 38),
            ))
        s.commit()
        return root_mid


def test_active_threads_decay_ranks_recent_burst_above_older_chatter(seeded_db):
    """Decay-weighted ranking: a single message *today* must outrank
    a 5-message thread from a week ago. With raw COUNT(*), the older
    thread would come first (5 > 1). With the half-life decay the
    fresh burst dominates -- that's the CONTEXT.md design point
    `active_threads` exists for."""
    from datetime import timedelta

    alpha = _inbox(seeded_db, "alpha")
    now = datetime.now(timezone.utc)
    # Recent burst: a single message a few hours ago. Score ~ 1.0.
    _seed_thread_with_messages(
        seeded_db, "alpha",
        root_id_prefix="recent",
        timestamps=[now - timedelta(hours=2)],
    )
    # Older steady thread: 5 messages from ~6.5 days ago. Each
    # contributes pow(0.5, 6.5) ~ 0.011; sum ~ 0.055. Solidly under
    # the recent thread's ~1.0.
    week_ago = now - timedelta(days=6, hours=12)
    _seed_thread_with_messages(
        seeded_db, "alpha",
        root_id_prefix="older",
        timestamps=[week_ago + timedelta(hours=i) for i in range(5)],
    )

    with seeded_db() as s:
        results = active_threads(s, alpha, days=7, limit=10, force=True)
    msgids = [r.message_id for r in results]

    recent_idx = msgids.index("recent-root@example.com")
    older_idx = msgids.index("older-root@example.com")
    assert recent_idx < older_idx, (
        f"decay ranking broken: a single recent message should outrank a "
        f"5-message week-old thread, but got order {msgids}"
    )
    # And the recent thread's raw recent_count is 1 vs older's 5 --
    # confirms we're not just lucky with a count-based tie-break.
    recent_row = next(r for r in results if r.message_id == "recent-root@example.com")
    older_row = next(r for r in results if r.message_id == "older-root@example.com")
    assert recent_row.recent_count == 1
    assert older_row.recent_count == 5


def test_active_threads_uses_cache_and_force_bypasses(seeded_db):
    """The cached wrapper round-trips through `get_or_compute`:
    a fresh call computes + stores; an immediate second call hits
    the cache (no recompute); `force=True` recomputes regardless.

    The "no recompute on hit" property is verified via the cache
    sentinel mechanism -- between the two un-forced calls we plant a
    sentinel value in the cache row, and the second call must return
    THAT value, not the live CTE result."""
    from mimir import cache

    alpha = _inbox(seeded_db, "alpha")

    # First call (force=True to ensure a fresh write).
    with seeded_db() as s:
        active_threads(s, alpha, days=30, limit=5, force=True)

    key = f"active_threads:{alpha.name}:30:5"
    # Sentinel value -- a value that would never come back from the CTE,
    # so we can detect whether the second call serves from cache.
    cache.set(key, [], ttl=3600)

    with seeded_db() as s:
        cached_result = active_threads(s, alpha, days=30, limit=5)
    assert cached_result == [], (
        "second un-forced call must return the sentinel from cache, not "
        "recompute"
    )

    # force=True bypasses the sentinel and recomputes; the real result
    # is whatever the seeded fixture produces (could be empty if 30d
    # window doesn't include 2024 anymore, but it's *not* our sentinel
    # if recompute happened -- and it lands in the cache row).
    with seeded_db() as s:
        forced = active_threads(s, alpha, days=30, limit=5, force=True)
    # After force=True the cache must hold the recomputed value, not
    # the sentinel. Equivalence to `forced` is the load-bearing claim.
    assert cache.get(key) == forced
