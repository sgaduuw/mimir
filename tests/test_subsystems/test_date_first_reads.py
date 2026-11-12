"""Date-first shape of the subsystem dashboard reads (#1156).

Active threads, daily volume and active reviewers filter by a short
date window. For an inbox with `INBOX_FIRST_BELOW_ARTICLES` or more
articles they must walk `ix_articles_date` over the window, not every
`article_lists` row of the inbox; smaller inboxes keep the inbox walk.
Both shapes must return the same rows.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from mimir.models import ArticleList, Inbox, Subsystem
from mimir.subsystems_dashboard import (
    active_reviewers_in_subsystem,
    active_threads_in_subsystem,
    daily_volume_in_subsystem,
)
from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats
from tests.test_subsystems._helpers import (
    _add_recent_patch_with_trailers,
    _add_recent_thread_root,
    _add_subsystem,
)

# (helper, a fragment unique to the statement it runs)
HELPERS = [
    (active_threads_in_subsystem, "WITH RECURSIVE chains"),
    (daily_volume_in_subsystem, "date(a.date) AS day"),
    (active_reviewers_in_subsystem, "COALESCE(m.address, t.address) AS address"),
]
IDS = ["active_threads", "daily_volume", "active_reviewers"]


def _force_shape(monkeypatch, inbox_first):
    from mimir.subsystems_dashboard import reads, reviewers

    for mod in (reads, reviewers):
        monkeypatch.setattr(mod, "_inbox_first", lambda _i: inbox_first)


def _seed(s):
    """Rows for every helper: in-window rows in alpha, a crosspost in
    both inboxes, a beta-only row, an out-of-window row, a row outside
    the subsystem."""
    sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
    mine = [("Reviewed-by", "Alice", "alice@kernel.org")]
    _add_recent_patch_with_trailers(s, "in1@x", ["fs/bcachefs/a.c"], mine)
    _add_recent_patch_with_trailers(
        s, "in2@x", ["fs/bcachefs/b.c"], mine + [("Acked-by", "Bob", "bob@kernel.org")]
    )
    cross = _add_recent_patch_with_trailers(
        s, "cross@x", ["fs/bcachefs/c.c"], [("Tested-by", "Cy", "cy@kernel.org")]
    )
    beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
    cross.lists.append(
        ArticleList(inbox_id=beta.id, epoch="0.git", commit_sha="e" * 40)
    )
    _add_recent_patch_with_trailers(
        s, "beta@x", ["fs/bcachefs/d.c"], mine, inbox_name="beta"
    )
    _add_recent_patch_with_trailers(s, "old@x", ["fs/bcachefs/e.c"], mine, days_ago=60)
    _add_recent_patch_with_trailers(s, "elsewhere@x", ["fs/btrfs/f.c"], mine)
    # A recent reply under an old root: the root joins the thread list.
    root = _add_recent_thread_root(s, "root@x", ["fs/btrfs/g.c"])
    root.date = datetime.now(UTC) - timedelta(days=20)
    reply = _add_recent_thread_root(s, "reply@x", ["fs/bcachefs/h.c"])
    reply.thread_parent = "root@x"
    s.flush()
    return sub


def _result(fn, s, inbox, sub):
    out = fn(s, inbox, sub, force=True)
    if fn is daily_volume_in_subsystem:
        return out.days
    if fn is active_reviewers_in_subsystem:
        return [(r.address_normalized, r.role_counts) for r in out]
    return [(t.message_id, t.recent_count, t.reply_count) for t in out]


@pytest.mark.parametrize("fn,_match", HELPERS, ids=IDS)
def test_both_shapes_return_the_same_rows(seeded_db, monkeypatch, fn, _match):
    with seeded_db() as s:
        sub = _seed(s)
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        seen = {}
        for shape in (False, True):
            _force_shape(monkeypatch, shape)
            seen[shape] = _result(fn, s, alpha, sub)
    assert seen[True] == seen[False]
    # Not vacuous, and beta's and the old rows stay out.
    if fn is active_threads_in_subsystem:
        assert {m for m, _, _ in seen[True]} == {
            "in1@x",
            "in2@x",
            "cross@x",
            "root@x",
        }
    elif fn is daily_volume_in_subsystem:
        assert sum(n for _, n in seen[True]) == 4
    else:
        assert {a for a, _ in seen[True]} == {
            "alice@kernel.org",
            "bob@kernel.org",
            "cy@kernel.org",
        }
        assert dict(seen[True])["alice@kernel.org"] == {"Reviewed-by": 2}


@pytest.mark.parametrize("inbox_first", [False, True])
@pytest.mark.parametrize("fn,match", HELPERS, ids=IDS)
def test_plan_follows_the_inbox_size(seeded_db, monkeypatch, fn, match, inbox_first):
    with seeded_db() as s:
        _seed(s)
        s.commit()
    _force_shape(monkeypatch, inbox_first)
    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        sub = s.execute(select(Subsystem)).scalars().one()
        plan = plan_of(s, lambda: fn(s, alpha, sub, force=True), match)
    lines = plan.splitlines()
    if inbox_first:
        assert "ix_articles_date" not in plan, plan
        assert any("ix_article_lists_inbox_id" in ln for ln in lines), plan
    else:
        assert any(
            ln.startswith("SEARCH a ") and "INDEX ix_articles_date" in ln
            for ln in lines
        ), plan
        assert "ix_article_lists_inbox_id" not in plan, plan
        assert "SCAN al" not in plan, plan
