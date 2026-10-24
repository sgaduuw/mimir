"""`backfill-patch-series` re-chains the keys it writes (#824).

An ingest batch can re-chain a key between the backfill's read and its
write. CONTEXT.md "Series chains" promises that a backfill planned from
an older read does not overwrite an ingest batch's heads, so the
backfill never writes a head from its read: each batch re-chains the
old and new key of every row it writes, on the writer.
"""

from datetime import UTC, datetime

from sqlalchemy import select

from mimir import _pending_backfill, patch_series_chains
from mimir._pending_backfill import _submit_patch_series_batch
from mimir.broker._context import get_active_writer
from mimir.broker.writes import WriteOp
from mimir.models import Article, ArticleList, Inbox
from mimir.patch_series import _process_one, backfill_patch_series, series_key
from mimir.patch_series_chains import backfill_series_chains, rechain_keys

KEY = series_key("netfilter updates", "A <a@x>")
OLD, NEW = datetime(2024, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)
MSGIDS = ["c1@x", "c1p1@x", "c1p2@x", "c2@x", "c2p1@x", "c2p2@x"]


def _add(s, inbox, msgid, subject, position, when, parent=None, key=KEY):
    s.add(
        Article(
            message_id=msgid,
            subject=subject,
            author="A <a@x>",
            date=when,
            thread_parent=parent,
            subject_normalized=subject.lower(),
            canonical_inbox_id=inbox.id,
            lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="d" * 40)],
            patch_series_key=key,
            patch_series_version="v1" if key else None,
            patch_series_position=position,
        )
    )


def _seed(
    seeded_db,
    c2_subject="[PATCH 0/2] netfilter updates",
    c2p2_key=KEY,
    c2p2_parent="c2@x",
    c2p2_subject="[PATCH 2/2] conntrack: zone limits",
):
    """Two postings under one title a year apart, with unrelated patch
    titles, so the key splits once chained."""
    with seeded_db() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        _add(s, inbox, "c1@x", "[PATCH 0/2] netfilter updates", 0, OLD)
        _add(s, inbox, "c1p1@x", "[PATCH 1/2] nft: add set gc", 1, OLD, "c1@x")
        _add(s, inbox, "c1p2@x", "[PATCH 2/2] nft: fix chain lookup", 2, OLD, "c1@x")
        _add(s, inbox, "c2@x", c2_subject, 0, NEW)
        _add(s, inbox, "c2p1@x", "[PATCH 1/2] ipvs: drop stale conn", 1, NEW, "c2@x")
        _add(
            s,
            inbox,
            "c2p2@x",
            c2p2_subject,
            2,
            NEW,
            c2p2_parent,
            key=c2p2_key,
        )
        s.commit()


def _heads(seeded_db):
    with seeded_db() as s:
        return dict(
            s.execute(
                select(Article.message_id, Article.patch_series_head_id).where(
                    Article.message_id.in_(MSGIDS)
                )
            ).all()
        )


def _read(seeded_db, msgid):
    """The backfill's read of one row, planned from the state before any
    chain pass (every head NULL)."""
    with seeded_db() as s:
        article = s.execute(
            select(Article).where(Article.message_id == msgid)
        ).scalar_one()
        _bucket, pending = _process_one(s, article, reprocess=True)
    assert pending is not None
    return pending


def _ingest_rechain():
    """An ingest batch re-chains the key on the writer, after the read."""
    get_active_writer().submit(
        WriteOp(label="test:ingest-rechain", fn=lambda conn: rechain_keys(conn, {KEY}))
    ).result(timeout=10)


def test_a_cover_rewrite_does_not_undo_a_re_chain_after_its_read(
    seeded_db, broker_active
):
    # The folded subject makes the backfill write the cover; unchanged,
    # the row would send no payload and the stale write would not happen.
    _seed(seeded_db, c2_subject="[PATCH 0/2] netfilter\n updates")
    pending = _read(seeded_db, "c2@x")

    _ingest_rechain()
    after_ingest = _heads(seeded_db)
    # Precondition: the key really split, so the newer posting has a head.
    assert after_ingest["c2@x"] is not None
    assert after_ingest["c2p1@x"] == after_ingest["c2@x"]

    _submit_patch_series_batch(get_active_writer(), [pending]).result(timeout=10)

    heads = _heads(seeded_db)
    # The cover must stay in the series its own patches are in.
    assert heads["c2@x"] == heads["c2p1@x"] == heads["c2p2@x"], heads


def test_a_linked_patch_does_not_take_a_head_from_its_read(seeded_db, broker_active):
    # c2p2 is an orphan the backfill links to c2.
    _seed(seeded_db, c2p2_key=None)
    pending = _read(seeded_db, "c2p2@x")

    _ingest_rechain()
    after_ingest = _heads(seeded_db)
    # Precondition: c2 split off on its linked patch's title alone.
    with seeded_db() as s:
        c2_id = s.execute(
            select(Article.id).where(Article.message_id == "c2@x")
        ).scalar_one()
    assert after_ingest["c2@x"] == after_ingest["c2p1@x"] == c2_id
    assert after_ingest["c2p2@x"] is None

    _submit_patch_series_batch(get_active_writer(), [pending]).result(timeout=10)

    heads = _heads(seeded_db)
    assert heads["c2p2@x"] == heads["c2@x"] == c2_id, heads


def test_a_second_reprocess_writes_and_re_chains_nothing(
    seeded_db, broker_active, monkeypatch
):
    """The cost guard: a row whose series columns do not change sends no
    payload, so a reprocess re-chains only the keys it really changes,
    not every key it walks."""
    _seed(seeded_db, c2p2_key=None)
    rechained: list[set[str]] = []
    payloads: list[list] = []

    def record_rechain(conn, keys):
        rechained.append(set(keys))
        return rechain_keys(conn, keys)

    def record_submit(writer, batch, after=None):
        payloads.append(list(batch))
        return _submit_patch_series_batch(writer, batch, after=after)

    monkeypatch.setattr(patch_series_chains, "rechain_keys", record_rechain)
    monkeypatch.setattr(_pending_backfill, "_submit_patch_series_batch", record_submit)

    backfill_patch_series(reprocess=True)
    # Precondition: the first run linked c2p2 and re-chained its key.
    assert any(KEY in keys for keys in rechained), rechained
    assert {p.article_id for batch in payloads for p in batch}

    rechained.clear()
    payloads.clear()
    backfill_patch_series(reprocess=True)

    assert [p for batch in payloads for p in batch] == []
    assert all(keys == set() for keys in rechained), rechained


def _id(seeded_db, msgid):
    with seeded_db() as s:
        return s.execute(
            select(Article.id).where(Article.message_id == msgid)
        ).scalar_one()


def test_the_re_chain_reaches_a_patch_that_answers_a_patch(seeded_db, broker_active):
    """#840: c2p2 answers c2p1, not the cover. The re-chain finds its
    cover by the same walk that links it, so it takes c2's head."""
    _seed(seeded_db, c2p2_parent="c2p1@x")
    backfill_series_chains()
    heads = _heads(seeded_db)
    assert heads["c2@x"] == _id(seeded_db, "c2@x")  # precondition: the key split
    assert heads["c2p2@x"] == heads["c2@x"]
    assert backfill_series_chains().heads_planned == 0


def test_a_subject_rewrite_keeps_a_chain_reply_patch_s_head(seeded_db, broker_active):
    """The #824 review's repro: a plain run rewrites c2p2's folded subject.
    Its batch writes head NULL, as every backfill write does, and the
    re-chain must reach c2p2 to put the head back."""
    _seed(
        seeded_db,
        c2p2_parent="c2p1@x",
        c2p2_subject="[PATCH 2/2] conntrack:\n zone limits",
    )
    backfill_series_chains()
    c2 = _id(seeded_db, "c2@x")
    with seeded_db() as s:
        # As ingest stored it before #840: its parent's head, copied.
        row = s.execute(
            select(Article).where(Article.message_id == "c2p2@x")
        ).scalar_one()
        row.patch_series_head_id = c2
        s.commit()

    backfill_patch_series()

    with seeded_db() as s:
        row = s.execute(
            select(Article).where(Article.message_id == "c2p2@x")
        ).scalar_one()
        assert "\n" not in row.subject  # precondition: the run wrote this row
        assert row.patch_series_head_id == c2
