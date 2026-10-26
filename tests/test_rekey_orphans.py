"""A later revision threaded under an older cover is re-keyed to its own
revision's cover (#871).

#862 orphans a patch that sits under a cover of another explicit version
and arrived more than an hour after it. When the patch's own version has
a cover among that old cover's direct replies (the author sent the new
posting with `--in-reply-to` the old cover), `cover_of_patch` returns it.
Ingest, a plain backfill run, `--reprocess` and the chain loader all call
it, so they agree.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from mimir.models import Article
from mimir.patch_series import (
    _posting,
    backfill_patch_series,
    cover_of_patch,
    series_key,
)
from mimir.patch_series_chains import _load_key
from tests.test_chain_reply_patches import AUTHOR, _msg, _series
from tests.test_ingest.test_series_rechain import T2021, T2026, _arrive, _heads

HOUR = 3600
DAY = 86400
KEY = series_key("t", AUTHOR)
PATCH = "[PATCH v3 1/2] foo"
BASE = datetime(2026, 8, 21, tzinfo=UTC)


def _row(mid, subject, parent, hours, author=AUTHOR):
    return SimpleNamespace(
        message_id=mid,
        subject=subject,
        author=author,
        thread_parent=parent,
        date=BASE + timedelta(hours=hours),
    )


def _link(rows, patch_hours=3, subject=PATCH):
    """`cover_of_patch` for a v3 patch answering `old@x`, over `rows`."""
    by_id = {r.message_id: r for r in rows}
    return cover_of_patch(
        "old@x",
        by_id.get,
        _posting(subject),
        subject,
        BASE + timedelta(hours=patch_hours),
        replies_of=lambda mid: [r for r in rows if r.thread_parent == mid],
    )


OLD = _row("old@x", "[PATCH v2 0/2] t", None, 0)
SIBLING = _row("sib@x", "[PATCH v3 0/2] t", "old@x", 2)


def test_the_own_version_cover_among_the_old_covers_replies_is_found():
    assert _link([OLD, SIBLING]) is SIBLING


@pytest.mark.parametrize(
    "sibling",
    [
        _row("sib@x", "[PATCH v3 0/3] t", "old@x", 2),  # another total
        _row("sib@x", "[PATCH v3 0/2] other", "old@x", 2),  # another key
        _row("sib@x", "[PATCH v3 0/2] t", "old@x", 2, author="z@b.example"),
        _row("sib@x", "[PATCH v4 0/2] t", "old@x", 2),  # another version
        _row("sib@x", "[PATCH 0/2] t", "old@x", 2),  # names no version
        _row("sib@x", "[PATCH v3 0/2] t", "old@x", 2 + 24 * 2),  # days away
        _row("sib@x", "[PATCH v3 0/2] t", "other@x", 2),  # not a reply of it
        _row("sib@x", "Re: [PATCH v3 0/2] t", "old@x", 2),  # not a cover
    ],
    ids=[
        "total",
        "title",
        "author",
        "version",
        "unversioned",
        "far",
        "elsewhere",
        "reply",
    ],
)
def test_a_cover_that_does_not_match_is_not_taken(sibling):
    assert sibling.thread_parent != "x"  # precondition: a real candidate row
    assert _link([OLD, sibling]) is None


def test_no_sibling_leaves_the_patch_orphaned():
    assert _link([OLD]) is None


def test_a_sibling_is_taken_within_a_day_on_either_side_of_the_patch():
    after = _row("sib@x", "[PATCH v3 0/2] t", "old@x", 3 + 20)
    before = _row("sib@x", "[PATCH v3 0/2] t", "old@x", 3 - 2)
    assert _link([OLD, after]) is after
    assert _link([OLD, before]) is before


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0)])
def test_the_earliest_sibling_wins_whatever_the_order(order):
    """By (date, message id): `a@x` and `b@x` tie on the date, so the id
    breaks it, and `c@x` is later than both."""
    rows = [
        _row("b@x", "[PATCH v3 0/2] t", "old@x", 2),
        _row("a@x", "[PATCH v3 0/2] t", "old@x", 2),
        _row("c@x", "[PATCH v3 0/2] t", "old@x", 3),
    ]
    got = _link([OLD, *(rows[i] for i in order)])
    assert got.message_id == "a@x"


def test_an_unrefused_cover_is_still_returned_and_replies_are_not_read():
    """Inside the hour, the old cover answers (#862, #870): the sibling
    lookup is for the refusal only."""
    rows = [OLD, SIBLING]
    by_id = {r.message_id: r for r in rows}

    def replies_of(mid):
        raise AssertionError("the sibling lookup ran without a refusal")

    got = cover_of_patch(
        "old@x",
        by_id.get,
        _posting(PATCH),
        PATCH,
        BASE + timedelta(minutes=5),
        replies_of=replies_of,
    )
    assert got is OLD


def _under_the_old_cover(extra=()):
    """v2 cover, then a v3 posting sent in reply to it: its own cover and
    two patches, one of them posted twice (a taken slot)."""
    return [
        _msg("old@x", "[PATCH v2 0/2] t", body=b"cover"),
        _msg("sib@x", "[PATCH v3 0/2] t", "old@x", body=b"cover"),
        _msg("p1@x", "[PATCH v3 1/2] foo", "old@x"),
        _msg("p1b@x", "[PATCH v3 1/2] foo", "old@x"),
        _msg("p2@x", "[PATCH v3 2/2] bar", "old@x"),
        *extra,
    ]


TIMES = [T2026, T2026 + 2 * HOUR, T2026 + 3 * HOUR, T2026 + 4 * HOUR, T2026 + 5 * HOUR]
LINKED = {
    "sib@x": (KEY, "v3", 0),
    "p1@x": (KEY, "v3", 1),
    "p1b@x": (KEY, "v3", 1),
    "p2@x": (KEY, "v3", 2),
}
ORPHANS = {
    "sib@x": (KEY, "v3", 0),
    "p1@x": (None, None, 1),
    "p1b@x": (None, None, 1),
    "p2@x": (None, None, 2),
}


def test_a_later_revision_under_an_old_cover_links_to_its_own_cover_at_ingest(
    seeded_db, tmp_path, broker_active
):
    """All in one batch, so the sibling cover is a pending row. A second
    post of 1/2 takes the same slot and is linked too, not refused."""
    _arrive(seeded_db, tmp_path, "0.git", T2026, _under_the_old_cover(), TIMES)
    assert _series(seeded_db, *LINKED) == LINKED
    heads = _heads(seeded_db)
    assert heads["sib@x"] == heads["p1@x"] == heads["p1b@x"] == heads["p2@x"]
    backfill_patch_series()
    backfill_patch_series(reprocess=True)
    assert _series(seeded_db, *LINKED) == LINKED
    assert _heads(seeded_db) == heads


def test_a_stored_orphan_is_re_linked_by_a_plain_run_once_the_cover_exists(
    seeded_db, tmp_path, broker_active
):
    """The cover of the new posting arrives after its patches: they are
    orphans until a plain run, which needs no `--reprocess`."""
    msgs = _under_the_old_cover()
    old, sib, *patches = msgs
    _arrive(seeded_db, tmp_path, "0.git", T2026, [old, *patches], TIMES[:1] + TIMES[2:])
    assert _series(seeded_db, "p1@x", "p1b@x", "p2@x") == {
        k: v for k, v in ORPHANS.items() if k != "sib@x"
    }  # precondition: orphaned, as #862 leaves them
    _arrive(seeded_db, tmp_path, "1.git", T2026 + 6 * HOUR, [sib])
    still = _series(seeded_db, "p1@x")
    assert still["p1@x"] == (None, None, 1)  # a cover alone re-keys nothing
    backfill_patch_series()
    assert _series(seeded_db, *LINKED) == LINKED
    heads = _heads(seeded_db)
    assert heads["sib@x"] == heads["p1@x"] == heads["p2@x"]


@pytest.mark.parametrize(
    "sibling",
    [
        ("[PATCH v3 0/3] t", 2 * HOUR),
        ("[PATCH v3 0/2] other title", 2 * HOUR),
        ("[PATCH v3 0/2] t", 2 * HOUR + 3 * 86400),
    ],
    ids=["total", "key", "far"],
)
def test_a_sibling_that_does_not_match_leaves_the_patches_orphaned(
    seeded_db, tmp_path, broker_active, sibling
):
    subject, delay = sibling
    msgs = _under_the_old_cover()
    msgs[1] = _msg("sib@x", subject, "old@x", body=b"cover")
    times = [TIMES[0], T2026 + delay, *TIMES[2:]]
    _arrive(seeded_db, tmp_path, "0.git", T2026, msgs, times)
    got = _series(seeded_db, "p1@x", "p1b@x", "p2@x")
    assert got == {k: v for k, v in ORPHANS.items() if k != "sib@x"}
    backfill_patch_series()
    backfill_patch_series(reprocess=True)
    assert _series(seeded_db, "p1@x", "p2@x")["p1@x"] == (None, None, 1)


def test_the_chain_loader_attributes_the_patch_to_the_sibling_cover(
    seeded_db, tmp_path, broker_active
):
    """`_load_key` walks with the same `cover_of_patch`, so the patch is
    counted as the sibling's, not skipped (which would leave it without a
    head or a posting version)."""
    _arrive(seeded_db, tmp_path, "0.git", T2026, _under_the_old_cover(), TIMES)
    with seeded_db() as s:
        patches = _load_key(s, KEY)[2]
        by_msgid = {
            s.scalars(select(Article.message_id).where(Article.id == pid)).one(): parent
            for pid, parent, _head, _version in patches
        }
    assert by_msgid == {"p1@x": "sib@x", "p1b@x": "sib@x", "p2@x": "sib@x"}


def test_a_target_in_a_later_chain_gives_the_patch_that_chains_head(
    seeded_db, tmp_path, broker_active
):
    """The key already holds a 2021 v3 (same title, patches that share
    nothing), so the 2026 v3, three days after the old cover, repeats a version
    and heads its own series. The patch
    takes that head, not the key's first series."""
    old = [
        _msg("y1@x", "[PATCH v3 0/2] t", body=b"cover"),
        _msg("y1-p1@x", "[PATCH v3 1/2] net: fix the frobnicator", "y1@x"),
        _msg("y1-p2@x", "[PATCH v3 2/2] net: add a knob", "y1@x"),
    ]
    _arrive(seeded_db, tmp_path, "0.git", T2021, old)
    msgs = [
        _msg("old@x", "[PATCH v2 0/2] t", body=b"cover"),
        _msg("sib@x", "[PATCH v3 0/2] t", "old@x", body=b"cover"),
        _msg("p1@x", "[PATCH v3 1/2] drm: rework the scheduler", "old@x"),
        _msg("p2@x", "[PATCH v3 2/2] drm: drop the lock", "old@x"),
    ]
    _arrive(
        seeded_db,
        tmp_path,
        "1.git",
        T2026,
        msgs,
        [T2026] + [T2026 + 3 * DAY + n * HOUR for n in (2, 3, 4)],
    )
    heads = _heads(seeded_db)
    assert heads["y1@x"] is None  # precondition: the first series
    assert heads["sib@x"] is not None  # precondition: a later chain
    assert heads["p1@x"] == heads["p2@x"] == heads["sib@x"]
    assert heads["p1@x"] != heads["y1@x"]


def test_the_replies_lookup_seeks_the_thread_parent_index(seeded_db):
    """AGENTS.md check 6: the SQL `_replies` runs, under production
    statistics. It runs once per refused patch on the ingest path."""
    from mimir.patch_series import _replies
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as s:
        plan = plan_of(s, lambda: _replies(s, "old@x"), "articles.thread_parent")
    assert "USING INDEX ix_articles_thread_parent (thread_parent=?)" in plan, plan
