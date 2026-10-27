"""Ingest re-chains the series keys a batch touches (#816).

A new cover can change which chain every other cover of its key is in:
an older posting arriving late takes the key's first series and pushes
the rest under a head. So each ingest batch, and each replay, recomputes
the whole of every key it touched, from the database, inside the same
transaction as its inserts, and must end where a fresh re-chain of every
key would.
"""

import html as html_lib
import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from mimir.ingest import ingest_epoch, replay_failures
from mimir.models import Article, Inbox, IngestState
from tests._helpers import rechain_all
from tests.test_ingest._helpers import _build_pubinbox_repo, _rfc5322, _subject

A = ["net: fix the frobnicator", "net: add a knob"]
B = ["drm: rework the scheduler", "drm: drop the lock"]
T2021 = int(datetime(2021, 3, 1, tzinfo=UTC).timestamp())
T2026 = int(datetime(2026, 8, 21, tzinfo=UTC).timestamp())


def _posting(tag, version, titles, *, first=1, total=None, body=b"x"):
    """A cover and its in-series patches, threaded under it as
    `git send-email` posts them. `first` is the first patch's position."""
    total = total or first + len(titles) - 1
    cover = _subject(
        _rfc5322(f"{tag}@x", body=body), f"[PATCH {version} 0/{total}] generic title"
    )
    patches = [
        _subject(
            _rfc5322(f"{tag}-p{n}@x", in_reply_to=f"{tag}@x"),
            f"[PATCH {version} {n}/{total}] {title}",
        )
        for n, title in enumerate(titles, first)
    ]
    return [cover, *patches]


# Two unrelated postings under one cover title, each sent as v1 then v2:
# the 2026 v1 is a version down from the 2021 v2 with disjoint patch
# titles, so it starts a second series.
OLD = _posting("old1", "v1", A) + _posting("old2", "v2", A)
NEW = _posting("new1", "v1", B) + _posting("new2", "v2", B)
OLD_IDS = [f"{t}{s}@x" for t in ("old1", "old2") for s in ("", "-p1", "-p2")]
NEW_IDS = [f"{t}{s}@x" for t in ("new1", "new2") for s in ("", "-p1", "-p2")]
SPLIT = {m: None for m in OLD_IDS} | {m: "new1@x" for m in NEW_IDS}


def _arrive(seeded_db, tmp_path, epoch, start, messages, times=None):
    """Ingest `messages` as epoch `epoch`, commit `i` dated `times[i]` or
    `start + i`."""
    mirror = tmp_path / "mirror"
    mirror.mkdir(exist_ok=True)
    _build_pubinbox_repo(mirror / epoch, messages, start=start, times=times)
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        alpha.mirror_path = str(mirror)
        s.commit()
        ingest_epoch(s, alpha, epoch, mirror / epoch, workers=1)
    return alpha


def _heads(seeded_db):
    """Message id of every keyed row -> message id of its series head
    (None: the key's first series)."""
    with seeded_db() as s:
        rows = s.execute(
            select(Article.id, Article.message_id, Article.patch_series_head_id).where(
                Article.patch_series_key.is_not(None)
            )
        ).all()
    by_id = {r.id: r.message_id for r in rows}
    return {
        r.message_id: by_id[r.patch_series_head_id] if r.patch_series_head_id else None
        for r in rows
    }


def _page(seeded_db, message_id):
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == message_id)
        ).scalar_one()
        return art.id, f"/alpha/{art.date.year}/{art.date.month:02d}/{art.id}"


def _fold_links(client, url):
    body = client.get(url).get_data(as_text=True)
    fold = body.split('class="revisions-fold"')[1].split("</details>")[0]
    return [
        html_lib.unescape(h) for h in re.findall(r'href="([^"]*/diff\?[^"]*)"', fold)
    ]


@pytest.mark.parametrize("first", ["old", "new"])
def test_arrival_order_does_not_change_the_heads(
    seeded_db, tmp_path, broker_active, first
):
    """Order independence. Old first, the 2026 posting splits off as it
    arrives; new first, it is the key's only series until the 2021 one
    arrives and takes the first series, so its rows are rewritten."""
    arrivals = {"old": (T2021, OLD), "new": (T2026, NEW)}
    for i, name in enumerate([first, "new" if first == "old" else "old"]):
        _arrive(seeded_db, tmp_path, f"{i}.git", *arrivals[name])
    assert _heads(seeded_db) == SPLIT
    # Ingest leaves the state a fresh re-chain would write: nothing to change.
    assert rechain_all(seeded_db) == (0, 0)


def test_the_oldest_series_keeps_its_diff_url_and_a_split_off_gets_s(
    client, seeded_db, tmp_path, broker_active
):
    """Test 8: the series holding the key's oldest cover keeps the diff
    URL it always had; the split-off one is spelled with `s=<head>`, and
    both serve."""
    _arrive(seeded_db, tmp_path, "0.git", T2021, OLD)
    _arrive(seeded_db, tmp_path, "1.git", T2026, NEW)
    new1_id, _ = _page(seeded_db, "new1@x")
    links = {
        mid: _fold_links(client, _page(seeded_db, mid)[1])
        for mid in ("old2@x", "new2@x")
    }
    queries = {mid: [link.split("?", 1)[1] for link in ls] for mid, ls in links.items()}
    assert queries == {
        "old2@x": ["from=v1&to=v2&pos=cover"],
        "new2@x": [f"from=v1&to=v2&pos=cover&s={new1_id}"],
    }
    for (link,) in links.values():
        assert client.get(link).status_code == 200


def test_a_re_chain_moves_the_etag_of_a_page_whose_revisions_did_not_change(
    client, seeded_db, tmp_path, broker_active
):
    """An older unrelated posting arrives with patches only at positions
    3 and 4. Patch 1 of the newer posting keeps the same revision rows,
    so only the re-chain, which moves it under a head, can move its
    page's ETag."""
    _arrive(seeded_db, tmp_path, "0.git", T2026, _posting("new1", "v1", B))
    new1_id, _ = _page(seeded_db, "new1@x")
    _, url = _page(seeded_db, "new1-p1@x")
    before = client.get(url)
    assert before.status_code == 200
    _arrive(seeded_db, tmp_path, "1.git", T2021, _posting("old", "v2", A, first=3))
    assert _heads(seeded_db)["new1-p1@x"] == "new1@x"
    after = client.get(url)
    assert after.status_code == 200
    assert before.headers["ETag"] != after.headers["ETag"]


def test_a_batch_whose_re_chain_raises_rolls_back_whole(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    """No provisional window: the inserts, the re-chain and the cursor
    commit together or not at all."""
    import mimir.patch_series_chains as chains
    from mimir.config import settings

    def boom(covers):
        raise RuntimeError("re-chain failed")

    monkeypatch.setattr(chains, "chain_series", boom)
    # One batch for the whole epoch, so there is no earlier batch to keep.
    monkeypatch.setattr(settings, "ingest_batch_flush_seconds", 3600.0)
    with pytest.raises(RuntimeError, match="re-chain failed"):
        _arrive(seeded_db, tmp_path, "0.git", T2021, OLD)
    with seeded_db() as s:
        assert (
            s.execute(select(Article.id).where(Article.message_id.in_(OLD_IDS))).all()
            == []
        )
        assert (
            s.execute(
                select(IngestState.last_commit_sha).where(IngestState.epoch == "0.git")
            ).all()
            == []
        )


def test_replay_re_chains_the_keys_it_recovers(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    """A v2 cover that failed to parse is recovered by replay after its
    v1 split off. Replay writes it with no head, so without the re-chain
    it would sit in the key's first series."""
    import mimir.parser

    _arrive(seeded_db, tmp_path, "0.git", T2021, OLD)
    _arrive(seeded_db, tmp_path, "1.git", T2026, _posting("new1", "v1", B))
    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 200)
    late = _posting("new2", "v2", [], total=2, body=b"x" * 500)
    alpha = _arrive(seeded_db, tmp_path, "2.git", T2026 + 7 * 86400, late)
    assert "new2@x" not in _heads(seeded_db)  # precondition: it failed to parse
    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 50_000_000)
    assert replay_failures(alpha).recovered == 1
    assert _heads(seeded_db)["new2@x"] == "new1@x"
    assert rechain_all(seeded_db) == (0, 0)


def test_a_patch_only_batch_re_chains_its_cover_s_key(
    seeded_db, tmp_path, broker_active
):
    """Pins the patch half of "a cover's key covers its patches": a 2026
    v1 cover lands alone, with no titles, so it joins the 2021 chain.
    Its patches land in a later batch with titles disjoint from the 2021
    posting's; only that batch's re-chain can split the cover off."""
    _arrive(seeded_db, tmp_path, "0.git", T2021, OLD)
    cover, *patches = _posting("new1", "v1", B)
    _arrive(seeded_db, tmp_path, "1.git", T2026, [cover])
    assert _heads(seeded_db)["new1@x"] is None  # precondition: joined, no titles
    _arrive(seeded_db, tmp_path, "2.git", T2026 + 60, patches)
    heads = _heads(seeded_db)
    assert heads["new1@x"] == heads["new1-p1@x"] == heads["new1-p2@x"] == "new1@x"
