"""`backfill-patch-series` and a reply stored as a cover (#820).

A reviewer's reply that repeats a cover's subject without "Re:" was
stored as a cover of its own key. `--reprocess` clears it, and a patch
threaded under such a reply is left without a series instead of being
linked to a key that has no cover.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from mimir.models import Article, ArticleList, Inbox
from mimir.patch_series import backfill_patch_series, series_key
from mimir.patch_series_chains import backfill_series_chains
from tests._helpers import series_columns

COVER = "[PATCH v2 0/2] improve foo\thandling"
REPLY = "[PATCH v2 0/2] improve foo handling"
AUTHOR = "Al <a@x>"
REVIEWER = "Rev <rev@x>"
KEY = series_key("improve foo handling", AUTHOR)
REPLY_KEY = series_key("improve foo handling", REVIEWER)


def _add(
    seeded_db,
    msgid,
    subject,
    author,
    *,
    parent=None,
    key=None,
    position=None,
    version="v2",
    when=datetime(2024, 6, 1, tzinfo=UTC),
):
    with seeded_db() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.add(
            Article(
                message_id=msgid,
                subject=subject,
                author=author,
                date=when,
                thread_parent=parent,
                subject_normalized=subject.lower(),
                canonical_inbox_id=inbox.id,
                lists=[
                    ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="d" * 40)
                ],
                patch_series_key=key,
                patch_series_version=version if key else None,
                patch_series_position=position,
            )
        )
        s.commit()


_series = series_columns


def test_reprocess_clears_a_stored_reply_cover(seeded_db, broker_active):
    _add(seeded_db, "c820@x", COVER, AUTHOR, key=KEY, position=0)
    _add(
        seeded_db, "r820@x", REPLY, REVIEWER, parent="c820@x", key=REPLY_KEY, position=0
    )
    # The author answering the reviewer the same way: a reply to a reply.
    _add(seeded_db, "rr820@x", REPLY, AUTHOR, parent="r820@x", key=KEY, position=0)
    # Guards: the sender's own re-post, and the next revision.
    _add(seeded_db, "again820@x", REPLY, AUTHOR, parent="c820@x", key=KEY, position=0)
    _add(
        seeded_db,
        "v3-820@x",
        "[PATCH v3 0/2] improve foo handling",
        REVIEWER,
        parent="c820@x",
    )

    result = backfill_patch_series(reprocess=True)

    got = _series(seeded_db, "c820@x", "r820@x", "rr820@x", "again820@x", "v3-820@x")
    assert got["c820@x"][:3] == (KEY, "v2", 0)
    assert got["r820@x"] == (None, None, None, None)
    assert got["rr820@x"] == (None, None, None, None)
    assert got["again820@x"][:3] == (KEY, "v2", 0)
    assert got["v3-820@x"][:3] == (REPLY_KEY, "v3", 0)
    assert result.reply_cover == 2


@pytest.mark.parametrize(
    ("reply", "writes"),
    [(REPLY, 0), ("[PATCH v2 0/2] improve foo\n handling", 1)],
    ids=["cleared", "folded"],
)
def test_a_plain_run_does_not_rewrite_a_cleared_reply(
    seeded_db, broker_active, reply, writes
):
    """A reply stored without a series, as ingest now writes it, is
    examined again by every plain run; it is written only when its
    folded subject needs rewriting (#710). Writes are counted by a
    trigger, so the test sees row UPDATEs, not payloads."""
    _add(seeded_db, "c820@x", COVER, AUTHOR, key=KEY, position=0)
    _add(seeded_db, "r820@x", reply, REVIEWER, parent="c820@x")
    with seeded_db() as s:
        s.execute(text("CREATE TABLE t820_writes (article_id INTEGER)"))
        s.execute(
            text(
                "CREATE TRIGGER t820_trg AFTER UPDATE ON articles BEGIN "
                "INSERT INTO t820_writes VALUES (NEW.id); END"
            )
        )
        s.commit()
    try:
        result = backfill_patch_series()
        with seeded_db() as s:
            written = s.execute(text("SELECT count(*) FROM t820_writes")).scalar_one()
            stored = s.execute(
                select(Article.subject).where(Article.message_id == "r820@x")
            ).scalar_one()
    finally:
        with seeded_db() as s:
            s.execute(text("DROP TRIGGER IF EXISTS t820_trg"))
            s.execute(text("DROP TABLE IF EXISTS t820_writes"))
            s.commit()
    assert result.reply_cover == 1  # precondition: the reply was recognised
    assert written == writes
    assert stored == REPLY
    assert _series(seeded_db, "r820@x")["r820@x"] == (None, None, None, None)


@pytest.mark.parametrize("reprocess", [False, True])
def test_a_patch_under_a_reply_cover_is_left_without_a_series(
    seeded_db, broker_active, reprocess
):
    """Without `--reprocess` the reply is stored as ingest now writes it
    (no series) and the patch is an orphan the run retries; with it, the
    reply is stored keyed, as before #820. Either way the patch must not
    take a key derived from the reply's subject."""
    _add(seeded_db, "c820@x", COVER, AUTHOR, key=KEY, position=0)
    reply_key = REPLY_KEY if reprocess else None
    _add(
        seeded_db,
        "r820@x",
        REPLY,
        REVIEWER,
        parent="c820@x",
        key=reply_key,
        position=0 if reprocess else None,
    )
    _add(
        seeded_db,
        "p1-820@x",
        "[PATCH v2 1/2] foo: add bar",
        REVIEWER,
        parent="r820@x",
        key=reply_key,
        position=1,
    )

    backfill_patch_series(reprocess=reprocess)

    got = _series(seeded_db, "r820@x", "p1-820@x")
    assert got["r820@x"] == (None, None, None, None)
    assert got["p1-820@x"] == (None, None, 1, None)


def test_chains_after_reprocess_keep_the_parent_key_s_heads(seeded_db, broker_active):
    """The operator step for #819 and #820 is `--reprocess` alone (#824).
    The reply's key loses its only cover; the cover's key keeps its
    heads."""
    _add(
        seeded_db,
        "v1-820@x",
        "[PATCH 0/2] improve foo handling",
        AUTHOR,
        key=KEY,
        position=0,
        version="v1",
    )
    _add(seeded_db, "c820@x", COVER, AUTHOR, parent="v1-820@x", key=KEY, position=0)
    _add(
        seeded_db, "r820@x", REPLY, REVIEWER, parent="c820@x", key=REPLY_KEY, position=0
    )
    backfill_series_chains()
    before = _series(seeded_db, "v1-820@x", "c820@x")

    backfill_patch_series(reprocess=True)

    assert _series(seeded_db, "v1-820@x", "c820@x") == before
    with seeded_db() as s:
        reply_key_covers = s.execute(
            select(Article.id).where(
                Article.patch_series_key == REPLY_KEY,
                Article.patch_series_position == 0,
            )
        ).all()
    assert reply_key_covers == []


def test_reprocess_re_chains_the_key_a_cleared_reply_leaves(seeded_db, broker_active):
    """#824: clearing a reply-cover writes its key NULL, so the batch
    must re-chain the key the row had, or that key keeps the split the
    reply caused. B's second posting splits off B's key on its patch
    titles and takes B's v2 into its chain; it is also a reply to A's
    cover. Once it is cleared, the v2 joins B's first posting."""
    b = "Bo <b@x>"
    b_key = series_key("widget rework", b)
    x1, x2 = datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 3, 1, tzinfo=UTC)
    _add(
        seeded_db,
        "ac@x",
        "[PATCH 0/2] widget rework",
        AUTHOR,
        key=series_key("widget rework", AUTHOR),
        position=0,
        version="v1",
        when=x2,
    )
    for msgid, subject, parent, position, when in [
        ("x1@x", "[PATCH 0/2] widget rework", None, 0, x1),
        ("x1p1@x", "[PATCH 1/2] widget: add foo", "x1@x", 1, x1),
        ("x1p2@x", "[PATCH 2/2] widget: drop bar", "x1@x", 2, x1),
        ("x2@x", "[PATCH 0/2] widget rework", "ac@x", 0, x2),
        ("x2p1@x", "[PATCH 1/2] gadget: rename qux", "x2@x", 1, x2),
        ("x2p2@x", "[PATCH 2/2] gadget: fix baz", "x2@x", 2, x2),
    ]:
        _add(
            seeded_db,
            msgid,
            subject,
            b,
            parent=parent,
            key=b_key,
            position=position,
            version="v1",
            when=when,
        )
    _add(
        seeded_db,
        "x3@x",
        "[PATCH v2 0/2] widget rework",
        b,
        key=b_key,
        position=0,
        when=datetime(2024, 3, 10, tzinfo=UTC),
    )
    backfill_series_chains()
    before = _series(seeded_db, "x2@x", "x3@x")
    with seeded_db() as s:
        x2_id = s.execute(
            select(Article.id).where(Article.message_id == "x2@x")
        ).scalar_one()
    # Precondition: the reply split off B's key and holds the v2.
    assert before["x2@x"][3] == before["x3@x"][3] == x2_id

    result = backfill_patch_series(reprocess=True)

    got = _series(seeded_db, "x1@x", "x2@x", "x3@x")
    assert result.reply_cover == 1
    assert got["x2@x"] == (None, None, None, None)
    assert got["x3@x"] == (b_key, "v2", 0, None)
    assert got["x1@x"] == (b_key, "v1", 0, None)
