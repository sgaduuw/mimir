"""In-series patches sent as a reply to the previous patch (#840).

`git send-email --chain-reply-to` threads 2/3 under 1/3 and 3/3 under
2/3, so only 1/3 answers the cover. Ingest, a plain backfill run and
`--reprocess` all link such a patch by walking its parents' subjects up to
the cover, and the re-chain gives it its cover's head.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from mimir import _pending_backfill, patch_series
from mimir.ingest import epoch as epoch_mod
from mimir.models import Article
from mimir.patch_series import (
    PATCH_WALK_HOPS,
    _posting,
    backfill_patch_series,
    cover_of_patch,
    in_series_link,
    series_key,
)
from mimir.patch_series_chains import _load_key, backfill_series_chains
from tests.test_ingest._helpers import _rfc5322
from tests.test_ingest.test_series_rechain import T2021, T2026, _arrive, _heads

AUTHOR = "a@b.example"
DIFF = b"diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-a\n+b\n"


def _msg(msgid, subject, parent=None, *, body=DIFF, sender=None):
    raw = _rfc5322(msgid, body=body, in_reply_to=parent)
    raw = raw.replace(b"Subject: t\r\n", f"Subject: {subject}\r\n".encode())
    if sender is not None:
        raw = raw.replace(b"From: a@b.example", f"From: {sender}".encode())
    return raw


def _chain(tag, version, titles, *, total=None, cover_title="generic title"):
    """A cover and its patches posted with `--chain-reply-to`: each patch
    answers the one before it, the first answers the cover."""
    total = total or len(titles)
    v = f"{version} " if version else ""
    msgs = [_msg(f"{tag}@x", f"[PATCH {v}0/{total}] {cover_title}", body=b"cover")]
    parent = f"{tag}@x"
    for n, title in enumerate(titles, 1):
        msgs.append(_msg(f"{tag}-p{n}@x", f"[PATCH {v}{n}/{total}] {title}", parent))
        parent = f"{tag}-p{n}@x"
    return msgs


def _series(seeded_db, *msgids):
    """message id -> (key, version, position)."""
    with seeded_db() as s:
        rows = s.execute(
            select(
                Article.message_id,
                Article.patch_series_key,
                Article.patch_series_version,
                Article.patch_series_position,
            ).where(Article.message_id.in_(msgids))
        ).all()
    return {r[0]: tuple(r[1:]) for r in rows}


def _record_payloads(monkeypatch):
    """Every payload the backfill sends to the writer, by message id."""
    sent: list = []
    submit = _pending_backfill._submit_patch_series_batch

    def record(writer, batch, after=None):
        sent.extend(batch)
        return submit(writer, batch, after=after)

    monkeypatch.setattr(_pending_backfill, "_submit_patch_series_batch", record)
    return sent


# --- The walk, on rows in memory ----------------------------------------------


def _rows(*specs):
    """`(message_id, subject, parent)` -> a counting `parent_of`."""
    rows = {
        m: SimpleNamespace(
            message_id=m, subject=s, author=AUTHOR, thread_parent=p, date=None
        )
        for m, s, p in specs
    }
    calls = []

    def parent_of(mid):
        calls.append(mid)
        return rows.get(mid)

    return parent_of, calls


def test_a_parent_cycle_ends_the_walk():
    """30 such cycles exist on production (2026-10-24). Each row is read
    once, then the repeat stops the walk."""
    parent_of, calls = _rows(
        ("a@x", "[PATCH 2/3] foo", "b@x"), ("b@x", "[PATCH 1/3] foo", "a@x")
    )
    assert (
        cover_of_patch(
            "a@x", parent_of, _posting("[PATCH 3/3] foo"), "[PATCH 3/3] foo", None
        )
        is None
    )
    assert calls == ["a@x", "b@x"]


def test_the_walk_stops_at_its_cap(monkeypatch):
    """A chain one row longer than the cap reads exactly the cap's rows
    and gives up; the same chain links under a larger cap, so the cap is
    the only reason it does not."""
    depth = PATCH_WALK_HOPS + 1
    specs = [("c@x", "[PATCH 0/9] t", None)]
    specs += [
        (f"p{i}@x", "[PATCH 1/9] foo", "c@x" if i == 0 else f"p{i - 1}@x")
        for i in range(depth - 1)
    ]
    parent_of, calls = _rows(*specs)
    posting = _posting("[PATCH 2/9] foo")
    start = f"p{depth - 2}@x"
    assert cover_of_patch(start, parent_of, posting, "[PATCH 2/9] foo", None) is None
    assert len(calls) == PATCH_WALK_HOPS
    calls.clear()
    monkeypatch.setattr(patch_series, "PATCH_WALK_HOPS", depth)
    found = cover_of_patch(start, parent_of, posting, "[PATCH 2/9] foo", None)
    assert found is not None and found.message_id == "c@x"
    assert len(calls) == depth


@pytest.mark.parametrize(
    "patch,middle",
    [
        ("[PATCH v2 2/2] foo", "[PATCH 2/2] foo"),
        ("[PATCH 4/6 v2] foo", "[PATCH 4/6] foo"),
        ("[PATCH 2/3] foo", "[PATCH 1/2] foo"),
    ],
    ids=["version", "trailing_version", "total"],
)
def test_a_patch_hop_needs_the_same_total_and_version(patch, middle):
    """A v2 patch answering v1's copy of it is a re-post, not the next
    patch of v1: linking it would put v2 under v1's key and version."""
    total = middle.split("/")[1].split("]")[0]
    parent_of, _ = _rows(("c@x", f"[PATCH 0/{total}] t", None), ("m@x", middle, "c@x"))
    assert cover_of_patch("m@x", parent_of, _posting(patch), patch, None) is None


def test_an_unversioned_patch_keeps_another_versions_cover():
    """Production holds 110,294 mostly genuine direct children whose
    cover names another version or total (2026-10-24); the cover they
    answer is theirs, as before."""
    parent_of, _ = _rows(("c@x", "[PATCH v2 0/3] t", None))
    assert in_series_link("[PATCH 1/3] foo", "c@x", parent_of, None) == (
        series_key("t", AUTHOR),
        "v2",
    )


def test_a_missing_date_keeps_the_cross_revision_link():
    """No date on the patch is no evidence: the cover's link stands."""
    parent_of, _ = _rows(("c@x", "[PATCH v2 0/3] t", None))
    assert in_series_link("[PATCH v3 1/3] foo", "c@x", parent_of, None) == (
        series_key("t", AUTHOR),
        "v2",
    )


# --- Ingest and the backfill agree --------------------------------------------


def test_a_chain_reply_series_is_unchanged_by_reprocess(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    """The issue's done-means: ingest links 2/3 and 3/3 through the patch
    before them, and `--reprocess` keeps them, writing nothing."""
    _arrive(seeded_db, tmp_path, "0.git", T2026, _chain("cr", "", ["a", "b", "c"]))
    ids = ["cr@x", "cr-p1@x", "cr-p2@x", "cr-p3@x"]
    before = _series(seeded_db, *ids)
    key = series_key("generic title", AUTHOR)
    assert before == {
        "cr@x": (key, "v1", 0),
        **{f"cr-p{n}@x": (key, "v1", n) for n in (1, 2, 3)},
    }
    sent = _record_payloads(monkeypatch)
    backfill_patch_series(reprocess=True)
    assert _series(seeded_db, *ids) == before
    with seeded_db() as s:
        mine = set(
            s.scalars(select(Article.id).where(Article.message_id.in_(ids))).all()
        )
    assert [p for p in sent if p.article_id in mine] == []


def test_one_plain_run_links_a_chain_that_arrived_out_of_order(
    seeded_db, tmp_path, broker_active
):
    """Production article ids 17798315 and 17798320: 3/6 arrived before
    2/6, so ingest could not link 3/6 to 6/6. One plain run links all of
    them, with no `--reprocess`."""
    full = _chain("oo", "", ["a", "b", "c", "d", "e", "f"])
    cover, p1, p2, *rest = full
    _arrive(seeded_db, tmp_path, "0.git", T2026, [cover, p1])
    _arrive(seeded_db, tmp_path, "1.git", T2026 + 10, rest)
    _arrive(seeded_db, tmp_path, "2.git", T2026 + 20, [p2])
    late = [f"oo-p{n}@x" for n in range(3, 7)]
    assert {v[0] for v in _series(seeded_db, *late).values()} == {None}  # precondition

    backfill_patch_series()

    key = series_key("generic title", AUTHOR)
    got = _series(seeded_db, *late)
    assert got == {f"oo-p{n}@x": (key, "v1", n) for n in range(3, 7)}


def test_a_shallow_chain_links_in_one_plain_run(seeded_db, tmp_path, broker_active):
    """3/3 under 1/3 under the cover, both stored unlinked (replay stores
    them so): one plain run links both, whatever order it meets them."""
    msgs = [
        _msg("sh@x", "[PATCH 0/3] shallow", body=b"cover"),
        _msg("sh-p1@x", "[PATCH 1/3] a", "sh@x"),
        _msg("sh-p3@x", "[PATCH 3/3] c", "sh-p1@x"),
    ]
    _arrive(seeded_db, tmp_path, "0.git", T2026, msgs)
    with seeded_db() as s:
        for row in s.scalars(
            select(Article).where(Article.message_id.in_(["sh-p1@x", "sh-p3@x"]))
        ):
            row.patch_series_key = row.patch_series_version = None
        s.commit()

    backfill_patch_series()

    key = series_key("shallow", AUTHOR)
    assert _series(seeded_db, "sh-p1@x", "sh-p3@x") == {
        "sh-p1@x": (key, "v1", 1),
        "sh-p3@x": (key, "v1", 3),
    }


@pytest.mark.parametrize(
    "cover,first,second",
    [
        ("[PATCH 0/2] t", "[PATCH 2/2] foo", "[PATCH v2 2/2] foo"),
        ("[PATCH 0/6] t", "[PATCH 4/6] foo", "[PATCH 4/6 v2] foo"),
    ],
    ids=["version", "trailing_version"],
)
def test_a_re_post_under_the_old_version_stays_an_orphan(
    seeded_db, tmp_path, broker_active, cover, first, second
):
    """In ingest and in both backfill modes: the guard is one rule."""
    position = int(second.split("/")[0].split()[-1])
    msgs = [
        _msg("gd@x", cover, body=b"cover"),
        _msg("gd-p@x", first, "gd@x"),
        _msg("gd-v2@x", second, "gd-p@x"),
    ]
    _arrive(seeded_db, tmp_path, "0.git", T2026, msgs)
    orphan = {"gd-v2@x": (None, None, position)}
    assert _series(seeded_db, "gd-v2@x") == orphan
    backfill_patch_series()
    assert _series(seeded_db, "gd-v2@x") == orphan
    backfill_patch_series(reprocess=True)
    assert _series(seeded_db, "gd-v2@x") == orphan


def test_an_unversioned_patch_under_a_v2_cover_stays_linked(
    seeded_db, tmp_path, broker_active
):
    msgs = [
        _msg("uv@x", "[PATCH v2 0/3] t", body=b"cover"),
        _msg("uv-p1@x", "[PATCH 1/3] foo", "uv@x"),
    ]
    _arrive(seeded_db, tmp_path, "0.git", T2026, msgs)
    linked = {"uv-p1@x": (series_key("t", AUTHOR), "v2", 1)}
    assert _series(seeded_db, "uv-p1@x") == linked
    backfill_patch_series(reprocess=True)
    assert _series(seeded_db, "uv-p1@x") == linked


# --- A later revision under an older cover (#862) -----------------------------

MINUTE = 60


@pytest.mark.parametrize(
    "cover,patch,delay,linked",
    [
        # v3 2/3 a day after a v2 0/3 cover: another revision's thread.
        ("[PATCH v2 0/3] t", "[PATCH v3 2/3] foo", 86400, False),
        ("[PATCH v2 0/3] t", "[PATCH v3 2/3] foo", 61 * MINUTE, False),
        # Within the hour, or the same minute (typos, per-patch versions).
        ("[PATCH v2 0/3] t", "[PATCH v3 2/3] foo", 59 * MINUTE, True),
        ("[PATCH v2 0/3] t", "[PATCH v3 2/3] foo", 5, True),
        # Same version, or a side that names none, is never a mismatch.
        ("[PATCH v2 0/3] t", "[PATCH v2 2/3] foo", 86400, True),
        ("[PATCH v2 0/3] t", "[PATCH 2/3] foo", 86400, True),
        ("[PATCH 0/3] t", "[PATCH v3 2/3] foo", 86400, True),
    ],
    ids=[
        "day",
        "past_hour",
        "within_hour",
        "same_minute",
        "same_version",
        "patch_unversioned",
        "cover_unversioned",
    ],
)
def test_a_patch_posted_later_under_another_revisions_cover(
    seeded_db, tmp_path, broker_active, cover, patch, delay, linked
):
    """Ingest, a plain run and `--reprocess` read the same stored dates."""
    cover_version = "v2" if "v2" in cover else "v1"
    _arrive(seeded_db, tmp_path, "0.git", T2026, [_msg("xr@x", cover, body=b"cover")])
    _arrive(
        seeded_db, tmp_path, "1.git", T2026 + delay, [_msg("xr-p@x", patch, "xr@x")]
    )
    expected = (
        (series_key("t", AUTHOR), cover_version, 2) if linked else (None, None, 2)
    )
    assert _series(seeded_db, "xr-p@x") == {"xr-p@x": expected}
    backfill_patch_series()
    assert _series(seeded_db, "xr-p@x") == {"xr-p@x": expected}
    backfill_patch_series(reprocess=True)
    assert _series(seeded_db, "xr-p@x") == {"xr-p@x": expected}


def test_a_stored_cross_revision_link_is_dropped_by_reprocess_and_the_rechain(
    seeded_db, tmp_path, broker_active
):
    """Stored as before the fix: the v3 patch carries the v2 cover's key.
    `_load_key` no longer counts it as that cover's patch, and
    `--reprocess` clears it."""
    msgs = [
        _msg("st@x", "[PATCH v2 0/3] t", body=b"cover"),
        _msg("st-p@x", "[PATCH v3 2/3] foo", "st@x"),
    ]
    _arrive(seeded_db, tmp_path, "0.git", T2026, msgs)
    key = series_key("t", AUTHOR)
    assert _series(seeded_db, "st-p@x")["st-p@x"] == (key, "v2", 2)  # same minute
    with seeded_db() as s:
        row = s.scalars(select(Article).where(Article.message_id == "st-p@x")).one()
        row.date = row.date + timedelta(days=1)
        s.commit()
        assert _load_key(s, key)[2] == []

    backfill_patch_series(reprocess=True)

    assert _series(seeded_db, "st-p@x") == {"st-p@x": (None, None, 2)}


HOUR = 3600


def test_in_batch_chain_reply_under_an_old_cover_is_orphaned(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    """Cover and a later v3 chain arrive in one batch: the cover is a
    pending row, not yet in the database. Both patches walk to it and
    compare their own date with the cover's, so neither is linked."""
    pending_hits = []
    real = epoch_mod._thread_parent

    def spy(session, mid, pending):
        row = real(session, mid, pending)
        pending_hits.append((mid, type(row).__name__))
        return row

    monkeypatch.setattr(epoch_mod, "_thread_parent", spy)
    _arrive(
        seeded_db,
        tmp_path,
        "0.git",
        T2026,
        [
            _msg("ib@x", "[PATCH v2 0/2] t", body=b"cover"),
            _msg("ib-p1@x", "[PATCH v3 1/2] a", "ib@x"),
            _msg("ib-p2@x", "[PATCH v3 2/2] b", "ib-p1@x"),
        ],
        times=[T2026, T2026 + 2 * HOUR, T2026 + 2 * HOUR + 1],
    )
    assert ("ib@x", "_ArticleInsert") in pending_hits
    assert _series(seeded_db, "ib-p1@x", "ib-p2@x") == {
        "ib-p1@x": (None, None, 1),
        "ib-p2@x": (None, None, 2),
    }
    backfill_patch_series(reprocess=True)
    assert _series(seeded_db, "ib-p1@x", "ib-p2@x") == {
        "ib-p1@x": (None, None, 1),
        "ib-p2@x": (None, None, 2),
    }


def test_reprocess_clears_the_head_of_a_dropped_link(
    seeded_db, tmp_path, broker_active
):
    """A split key: the 2026 posting heads its own series. Its stale v3
    patch, stored with that head, loses key and head on `--reprocess`, and
    the 2026 posting keeps its head (the re-chain of the old key, #824)."""
    old = _chain("old", "v2", ["net: fix the frobnicator", "net: add a knob"])
    _arrive(seeded_db, tmp_path, "0.git", T2021, old)
    _arrive(
        seeded_db,
        tmp_path,
        "1.git",
        T2026,
        [
            _msg("nw@x", "[PATCH v2 0/2] generic title", body=b"cover"),
            _msg("nw-p1@x", "[PATCH v2 1/2] drm: rework the scheduler", "nw@x"),
            _msg("nw-p2@x", "[PATCH v3 2/2] drm: drop the lock", "nw@x"),
        ],
    )
    before = _heads(seeded_db)
    assert before["nw@x"] == before["nw-p2@x"] == "nw@x"  # the key split
    with seeded_db() as s:
        row = s.scalars(select(Article).where(Article.message_id == "nw-p2@x")).one()
        row.date = row.date.replace(year=row.date.year + 1)
        s.commit()
    backfill_patch_series(reprocess=True)
    after = _heads(seeded_db)
    assert after["nw@x"] == after["nw-p1@x"] == "nw@x"
    with seeded_db() as s:
        row = s.execute(
            select(Article.patch_series_key, Article.patch_series_head_id).where(
                Article.message_id == "nw-p2@x"
            )
        ).one()
    assert tuple(row) == (None, None)


# --- Heads under a split key --------------------------------------------------

# A 2021 posting, then a 2026 one under the same cover title whose patch
# titles share nothing with it, so the key splits and the 2026 posting
# takes its cover as head.
OLD = _chain("old", "", ["net: fix the frobnicator", "net: add a knob"])


def test_a_chain_reply_patch_takes_its_cover_s_head_at_ingest(
    seeded_db, tmp_path, broker_active
):
    """Ingest no longer copies a head: the batch's re-chain writes it, and
    it now reaches 2/2, which answers 1/2 rather than the cover."""
    _arrive(seeded_db, tmp_path, "0.git", T2021, OLD)
    new = _chain("new", "", ["drm: rework the scheduler", "drm: drop the lock"])
    _arrive(seeded_db, tmp_path, "1.git", T2026, new)
    heads = _heads(seeded_db)
    assert heads["new@x"] == "new@x"  # precondition: the key split
    assert heads["new-p1@x"] == heads["new-p2@x"] == "new@x"
    assert backfill_series_chains().heads_planned == 0


def test_titles_of_chain_reply_patches_split_two_postings(
    seeded_db, tmp_path, broker_active
):
    """The re-chain compares patch titles. Read only from the cover's
    direct children, two postings a year apart share their 1/3 title and
    join; read through the chain, 2/3 and 3/3 differ and they split."""
    first = _chain("y1", "", ["mm: common start", "mm: alpha", "mm: beta"])
    second = _chain("y2", "", ["mm: common start", "io: gamma", "io: delta"])
    _arrive(seeded_db, tmp_path, "0.git", T2021, first)
    _arrive(seeded_db, tmp_path, "1.git", T2026, second)
    heads = _heads(seeded_db)
    assert heads["y1@x"] is None
    assert heads["y2@x"] == heads["y2-p3@x"] == "y2@x"


def test_a_re_post_links_through_a_cleared_reply(seeded_db, tmp_path, broker_active):
    """cover <- 1/2 <- a reviewer's reply repeating 1/2 with no diff
    (cleared by #837, so it carries no key) <- the author's re-post of 1/2
    with a diff. The re-post links, and under a split key the re-chain
    reaches it only by reading the cleared reply from the database."""
    _arrive(seeded_db, tmp_path, "0.git", T2021, OLD)
    msgs = [
        _msg("rp@x", "[PATCH 0/2] generic title", body=b"cover"),
        _msg("rp-p1@x", "[PATCH 1/2] drm: rework the scheduler", "rp@x"),
        _msg(
            "rp-r@x",
            "[PATCH 1/2] drm: rework the scheduler",
            "rp-p1@x",
            body=b"Looks good.",
            sender="reviewer@x.example",
        ),
        _msg("rp-re@x", "[PATCH 1/2] drm: rework the scheduler", "rp-r@x"),
    ]
    _arrive(seeded_db, tmp_path, "1.git", T2026, msgs)
    key = series_key("generic title", AUTHOR)
    got = _series(seeded_db, "rp-r@x", "rp-re@x")
    assert got["rp-r@x"] == (None, None, None)  # precondition: #837 cleared it
    assert got["rp-re@x"] == (key, "v1", 1)
    heads = _heads(seeded_db)
    assert heads["rp@x"] == "rp@x"  # precondition: the key split
    assert heads["rp-re@x"] == "rp@x"
    assert backfill_series_chains().heads_planned == 0


def test_the_re_chain_reads_a_key_s_patches_by_the_key_index(seeded_db):
    """AGENTS.md check 6: every ingest batch re-chains its keys, so this
    read runs on the ingest path. Production's row for the key index, read
    2026-10-24; its 66 averages over NULL keys too."""
    from mimir.patch_series_chains import head_updates
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    stats = (
        *PRODUCTION_STATS,
        ("articles", "ix_articles_patch_series_key", "17797373 66"),
    )
    key = series_key("generic title", AUTHOR)
    with planner_stats(seeded_db, stats), seeded_db() as s:
        plan = plan_of(
            s, lambda: head_updates(s, key), "articles.patch_series_position >"
        )
    assert "USING INDEX ix_articles_patch_series_key (patch_series_key=?)" in plan, plan
