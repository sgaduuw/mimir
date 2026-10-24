"""Series chains (#816): `chain_series` cuts one key's covers into the
postings a sender made, and `backfill-series-chains` writes the heads.

The pure rule is tested on `Cover` values. The backfill is tested on
real rows, keyed by `backfill_patch_series` first, the way production
gets them."""

import itertools
import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from mimir import backfill_runs
from mimir.cli import backfill_series_chains_command
from mimir.models import Article, ArticleList, Inbox
from mimir.patch_series import backfill_patch_series, newer_revision_sql
from mimir.patch_series_chains import Cover, backfill_series_chains, chain_series
from tests.test_backfill_runs import (
    _examined,
    _fail_batch,
    _invoke,
    _resumed_at,
    _runs,
    _small_batches,
)

T0 = datetime(2024, 1, 1)
A = ["net: fix the frobnicator", "net: add a knob"]
B = ["drm: rework the scheduler", "drm: drop the lock"]


@pytest.fixture(autouse=True)
def _no_live_window(monkeypatch):
    # The tests resume a run at once; the liveness window would refuse it.
    monkeypatch.setattr(backfill_runs, "LIVE_RUN_SEC", 0)


def _cover(i, days, version="v1", titles=(), subject=None, message_id=None):
    return Cover(
        id=i,
        message_id=message_id or f"c{i}@x",
        date=None if days is None else T0 + timedelta(days=days),
        version=version,
        subject=subject or f"[PATCH {version} 0/2] generic title",
        patch_titles=tuple(titles),
    )


# ----- the rule ---------------------------------------------------------


def test_same_title_weeks_apart_with_disjoint_patches_are_two_chains():
    covers = [
        _cover(1, 0, "v2", ["net: fix the frobnicator", "net: add a knob"]),
        _cover(2, 30, "v1", ["drm: rework the scheduler", "drm: drop the lock"]),
    ]
    assert chain_series(covers) == {1: None, 2: 2}


def test_versions_going_up_are_one_chain_whatever_the_titles():
    covers = [
        _cover(1, 0, "v1", ["a: one", "a: two"]),
        _cover(2, 20, "v2", ["a: one", "a: two"]),
        _cover(3, 40, "v3", ["b: three", "b: four"]),
    ]
    assert chain_series(covers) == {1: None, 2: None, 3: None}


def test_same_version_within_a_day_is_one_chain():
    covers = [
        _cover(1, 0, titles=["net: fix the frobnicator"]),
        _cover(2, 0.5, titles=["drm: rework the scheduler"]),
    ]
    assert chain_series(covers) == {1: None, 2: None}


def test_same_version_weeks_later_joins_only_when_titles_match():
    first = _cover(1, 0, titles=["net: fix the frobnicator", "net: add a knob"])
    again = _cover(2, 21, titles=["net: fix the frobnicator", "net: add a knob"])
    other = _cover(3, 21, titles=["drm: rework the scheduler", "drm: drop the lock"])
    assert chain_series([first, again]) == {1: None, 2: None}
    assert chain_series([first, other]) == {1: None, 3: 3}


def test_rfc_then_v1_is_a_version_up():
    covers = [
        _cover(1, 0, "rfc", ["a: one"], subject="[RFC 0/2] generic title"),
        _cover(2, 60, "v1", ["b: two"], subject="[PATCH 0/2] generic title"),
    ]
    assert chain_series(covers) == {1: None, 2: None}


def test_v1_after_an_rfc_v2_is_a_version_up():
    # An RFC ranks below any non-RFC version, even an RFC v2 below v1.
    covers = [
        _cover(1, 0, "v2", ["a: one"], subject="[PATCH RFC v2 0/2] generic title"),
        _cover(2, 60, "v1", ["b: two"], subject="[PATCH 0/2] generic title"),
    ]
    assert chain_series(covers) == {1: None, 2: None}


def test_resend_joins_the_version_it_resends():
    covers = [
        _cover(1, 0, "v3", ["a: one", "a: two"]),
        _cover(
            2,
            90,
            "v3",
            ["b: three", "b: four"],
            subject="[PATCH RESEND v3 0/2] generic title",
        ),
    ]
    assert chain_series(covers) == {1: None, 2: None}


def test_a_cover_with_no_patch_titles_joins_and_does_not_split():
    bare = _cover(2, 90)
    chain = [_cover(1, 0, titles=["a: one", "a: two"])]
    assert chain_series([*chain, bare]) == {1: None, 2: None}
    # The next cover compares with the last two postings, one of which
    # has titles; the bare one never makes a chain "empty" for it.
    later = _cover(3, 180, titles=["z: unrelated", "z: other"])
    assert chain_series([*chain, bare, later]) == {1: None, 2: None, 3: 3}
    assert chain_series([bare, _cover(3, 180, titles=["z: unrelated"])]) == {
        2: None,
        3: None,
    }


def test_reworded_patch_titles_still_match_by_word():
    a = "clk: npcm750: Add binding for Nuvoton NPCM7XX clock"
    b = "dt-binding: clk: npcm750: Add binding for Nuvoton NPCM7XX Clock"
    covers = [_cover(1, 0, titles=[a]), _cover(2, 30, titles=[b])]
    assert chain_series(covers) == {1: None, 2: None}


def test_split_needs_the_matched_share_below_the_threshold(monkeypatch):
    # Of the new cover's two titles one matches: a share of 0.5.
    chain = _cover(1, 0, titles=["a: one thing here", "b: another thing here"])
    new = _cover(2, 30, titles=["a: one thing here", "z: nothing alike zz"])
    from mimir import patch_series_chains as m

    assert chain_series([chain, new]) == {1: None, 2: None}
    monkeypatch.setattr(m, "SPLIT_BELOW_MATCHED_SHARE", 0.51)
    assert chain_series([chain, new]) == {1: None, 2: 2}


def test_a_single_cover_is_the_key_s_first_chain():
    assert chain_series([_cover(7, 0, titles=[f"p: {n}" for n in range(30)])]) == {
        7: None
    }


def test_matched_share_is_over_the_smaller_list():
    # A resend that keeps 2 of 6 patches: every title it has matches.
    six = [f"mm: change number {n} of the allocator" for n in range(6)]
    covers = [_cover(1, 0, titles=six), _cover(2, 30, titles=six[:2])]
    assert chain_series(covers) == {1: None, 2: None}


def test_only_the_last_two_postings_are_compared():
    # The third cover matches the first posting only, which has left the
    # window of the chain's last two.
    a, b, c = (["x: alpha one two", "x: alpha three four"], ["y: beta"], ["z: gamma"])
    covers = [
        _cover(1, 0, "v1", a),
        _cover(2, 0.2, "v1", b),
        _cover(3, 0.4, "v1", c),
        _cover(4, 30, "v1", a),
    ]
    assert chain_series(covers) == {1: None, 2: None, 3: None, 4: 4}


def test_dateless_covers_sort_first():
    covers = [_cover(1, None, "v3", A), _cover(2, 0, "v1", B), _cover(3, 30, "v2", B)]
    assert chain_series(covers) == {1: None, 2: 2, 3: 2}


def test_a_dateless_cover_of_the_same_version_joins():
    covers = [_cover(1, 0, "v1", A), _cover(2, None, "v1", B)]
    assert chain_series(covers) == {1: None, 2: None}


def test_same_version_exactly_one_day_later_joins():
    assert chain_series([_cover(1, 0, "v1", A), _cover(2, 1, "v1", B)]) == {
        1: None,
        2: None,
    }


def test_same_version_is_compared_with_the_chain_max_not_the_latest():
    # v1 joins after v2 by title, then v1 again within a day. The chain's
    # max is v2, so the repeat is not a same-version resend (Sherlock's
    # prototype, as the production counts were measured), and its titles
    # decide.
    covers = [
        _cover(1, 0, "v1", A),
        _cover(2, 10, "v2", A),
        _cover(3, 40, "v1", A),
        _cover(4, 40.5, "v1", B),
    ]
    assert chain_series(covers) == {1: None, 2: None, 3: None, 4: 4}


def test_version_up_is_above_the_chain_max_not_its_latest():
    covers = [_cover(1, 0, "v2", A), _cover(2, 30, "v1", A), _cover(3, 60, "v2", B)]
    assert chain_series(covers) == {1: None, 2: None, 3: 3}


def test_a_list_tag_resend_joins():
    covers = [
        _cover(1, 0, "v3", A),
        _cover(2, 90, "v3", B, subject="[RESEND] [PATCH v3 0/2] generic title"),
    ]
    assert chain_series(covers) == {1: None, 2: None}


def test_the_result_is_a_function_of_the_set_of_covers():
    covers = [
        _cover(1, 0, "v2", ["a: one", "a: two"]),
        # Equal dates: message-id decides the order, not the article id.
        _cover(9, 40, "v1", ["b: three", "b: four"], message_id="a@x"),
        _cover(3, 40, "v1", ["b: three", "b: four"], message_id="b@x"),
        _cover(4, 41.5, "v1", ["c: five", "c: six"]),
        _cover(5, 200, "v1", ["d: seven", "d: eight"]),
    ]
    expected = chain_series(covers)
    assert expected[9] == 9 and expected[3] == 9, expected  # head is not min id
    assert len(set(expected.values())) > 2, expected  # the fixture does split
    orders = list(itertools.permutations(covers))
    random.Random(816).shuffle(orders)
    for order in orders[:60]:
        assert chain_series(order) == expected


# ----- the backfill -----------------------------------------------------


def _seed(seeded_db, mid, subject, when, parent=None, author="A <a@x>"):
    with seeded_db() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        art = Article(
            message_id=mid,
            subject=subject,
            author=author,
            date=when.replace(tzinfo=UTC),
            thread_parent=parent,
            subject_normalized=subject.lower(),
            canonical_inbox_id=inbox.id,
            lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="d" * 40)],
        )
        s.add(art)
        s.commit()
        return art.id


def _posting(seeded_db, mid, version, when, patches):
    """A cover, `patches` titles under it, as an unthreaded archive row."""
    cover = _seed(seeded_db, mid, f"[PATCH {version} 0/{len(patches)}] generic", when)
    for n, title in enumerate(patches, 1):
        _seed(
            seeded_db,
            f"{mid}-p{n}",
            f"[PATCH {version} {n}/{len(patches)}] {title}",
            when,
            parent=mid,
        )
    return cover


def _heads(seeded_db):
    with seeded_db() as s:
        return {
            r.message_id: r.patch_series_head_id
            for r in s.execute(
                select(Article.message_id, Article.patch_series_head_id).where(
                    Article.patch_series_key.is_not(None)
                )
            )
        }


def _superseded(seeded_db):
    """Covers some other cover supersedes, by the shared predicate."""
    with seeded_db() as s:
        rows = s.execute(
            text(
                "SELECT DISTINCT o.message_id FROM articles o JOIN articles n ON "
                + newer_revision_sql("o", "n")
                + " WHERE o.patch_series_position = 0"
            )
        )
        return {r[0] for r in rows}


def _key_unchained(seeded_db):
    """Key the corpus as history ingested before 3.25.0 holds it: series
    columns set, no heads. `backfill_patch_series` chains the keys it
    writes (#824), so its heads are cleared."""
    backfill_patch_series()
    with seeded_db() as s:
        s.execute(text("UPDATE articles SET patch_series_head_id = NULL"))
        s.commit()


def _two_unrelated_postings(seeded_db):
    old = _posting(seeded_db, "old@x", "v2", datetime(2021, 3, 1), ["net: a", "net: b"])
    new = _posting(
        seeded_db, "new@x", "v1", datetime(2026, 8, 21), ["drm: c unlike", "drm: d"]
    )
    _key_unchained(seeded_db)
    return old, new


def test_unrelated_same_title_postings_are_not_superseded(seeded_db, broker_active):
    _two_unrelated_postings(seeded_db)
    # Precondition: on the bare key the 2026 v1 reads as superseded by the 2021 v2.
    assert _superseded(seeded_db) == {"new@x"}

    result = backfill_series_chains()

    assert (result.keys_chained, result.keys_split) == (1, 1)
    assert _superseded(seeded_db) == set()
    heads = _heads(seeded_db)
    assert heads["old@x"] is None and heads["old@x-p1"] is None
    with seeded_db() as s:
        new_id = s.execute(
            select(Article.id).where(Article.message_id == "new@x")
        ).scalar()
    # The patches follow their cover.
    assert heads["new@x"] == heads["new@x-p1"] == heads["new@x-p2"] == new_id


def test_v1_v2_v3_unthreaded_stay_one_series_with_v1_and_v2_superseded(
    seeded_db, broker_active
):
    for n, v in enumerate(("v1", "v2", "v3")):
        _posting(
            seeded_db, f"s{v}@x", v, datetime(2024, 5, 1 + 10 * n), ["a: one", "a: two"]
        )
    backfill_patch_series()

    result = backfill_series_chains()

    assert (result.keys_chained, result.keys_split, result.heads_planned) == (1, 0, 0)
    assert _superseded(seeded_db) == {"sv1@x", "sv2@x"}


def test_a_big_single_posting_is_one_chain_and_writes_no_head(seeded_db, broker_active):
    _posting(
        seeded_db,
        "big@x",
        "v1",
        datetime(2024, 5, 1),
        [f"mm: patch {n}" for n in range(40)],
    )
    backfill_patch_series()

    result = backfill_series_chains()

    assert (result.keys_chained, result.heads_planned) == (0, 0)
    assert set(_heads(seeded_db).values()) == {None}


def test_reprocess_then_the_chain_backfill_is_idempotent(seeded_db, broker_active):
    _two_unrelated_postings(seeded_db)
    backfill_series_chains()
    first = _heads(seeded_db)
    assert first["new@x"] is not None  # the split is what we check survives

    backfill_patch_series(reprocess=True)
    second_run = backfill_series_chains()

    assert _heads(seeded_db) == first
    assert second_run.heads_planned == 0
    assert backfill_series_chains().heads_planned == 0


def test_a_lone_cover_loses_a_stale_head_and_so_do_its_patches(
    seeded_db, broker_active
):
    _posting(seeded_db, "lone@x", "v1", datetime(2024, 5, 1), ["a: one", "a: two"])
    backfill_patch_series()
    with seeded_db() as s:
        # A head left by an earlier chaining of a key that has since shrunk.
        s.execute(
            text(
                "UPDATE articles SET patch_series_head_id = 4242 WHERE patch_series_key IS NOT NULL"
            )
        )
        s.commit()
    assert set(_heads(seeded_db).values()) == {4242}

    result = backfill_series_chains()

    assert result.heads_planned == 3  # the cover and its two patches
    assert set(_heads(seeded_db).values()) == {None}


def test_only_stale_children_are_rewritten(seeded_db, broker_active):
    _two_unrelated_postings(seeded_db)
    backfill_series_chains()
    with seeded_db() as s:
        s.execute(
            text(
                "UPDATE articles SET patch_series_head_id = NULL WHERE message_id = 'new@x-p2'"
            )
        )
        s.commit()

    result = backfill_series_chains()

    assert result.heads_planned == 1
    assert _heads(seeded_db)["new@x-p2"] == _heads(seeded_db)["new@x"]


def test_a_key_is_chained_once_per_chunk_however_many_covers_it_has(
    seeded_db, broker_active
):
    for n in range(5):
        _posting(
            seeded_db,
            f"m{n}@x",
            "v1",
            datetime(2024, 1, 1) + timedelta(days=60 * n),
            [f"t{n}: x{n} y{n}"],
        )
    backfill_patch_series()

    result = backfill_series_chains()

    assert result.keys_chained == 1  # not 5
    assert result.skipped >= 4


# ----- the machinery ----------------------------------------------------


def _split_corpus(seeded_db):
    """Three keys, each split in two; the cover ids come back newest first."""
    ids = []
    for k in range(3):
        author = f"K{k} <k{k}@x>"
        for n, (v, when) in enumerate(
            (("v2", datetime(2021, 1, 1)), ("v1", datetime(2026, 1, 1)))
        ):
            mid = f"k{k}n{n}@x"
            ids.append(
                _seed(seeded_db, mid, f"[PATCH {v} 0/1] generic", when, author=author)
            )
            _seed(
                seeded_db,
                mid + "-p",
                f"[PATCH {v} 1/1] w{k}{n}: x{k}{n} y{k}{n}",
                when,
                parent=mid,
                author=author,
            )
    _key_unchained(seeded_db)
    return ids


def _split_count(seeded_db):
    with seeded_db() as s:
        return s.execute(
            select(Article.id).where(Article.patch_series_head_id.is_not(None))
        ).all()


def test_cli_chains_and_finishes_its_run(seeded_db, broker_active):
    _split_corpus(seeded_db)

    result = _invoke(backfill_series_chains_command)

    assert result.exit_code == 0, result.output
    assert len(_split_count(seeded_db)) == 6  # 3 split covers + their 3 patches
    (name, _key, _cursor, _examined_total, finished) = _runs(seeded_db)[-1]
    assert (name, finished) == ("backfill_series_chains", True)


def test_a_limited_run_resumes_below_its_cursor(seeded_db, broker_active, monkeypatch):
    _split_corpus(seeded_db)
    _small_batches(monkeypatch)

    limited = _invoke(backfill_series_chains_command, "--limit", "4")
    assert limited.exit_code == 0, limited.output
    assert _examined(limited.output) == 4
    assert 0 < len(_split_count(seeded_db)) < 6
    assert _runs(seeded_db)[-1][4] is False

    rest = _invoke(backfill_series_chains_command)
    assert rest.exit_code == 0, rest.output
    assert _resumed_at(rest.output) is not None
    assert len(_split_count(seeded_db)) == 6


def test_an_ingest_re_chain_between_read_and_write_is_not_overwritten(
    seeded_db, broker_active, monkeypatch
):
    """The backfill reads a key, then writes in a later WriteOp. An
    ingest batch can re-chain that key in between: here a 2024 v1 with
    the 2026 posting's patch titles arrives, and the 2026 posting joins
    its chain. Heads planned from the earlier read would put the 2026
    posting back under its own head, so the write re-chains on the
    writer instead."""
    import mimir._pending_backfill as pending
    from mimir.broker.writes import WriteOp
    from mimir.patch_series_chains import rechain_keys

    _two_unrelated_postings(seeded_db)
    with seeded_db() as s:
        key = s.execute(
            select(Article.patch_series_key).where(Article.message_id == "new@x")
        ).scalar()
    real_submit = pending._submit_series_heads_batch
    calls = {"n": 0}

    def ingest_lands_first(writer, payloads, after=None):
        calls["n"] += 1
        if calls["n"] == 1:
            _posting(
                seeded_db,
                "x@x",
                "v1",
                datetime(2024, 1, 1),
                ["drm: c unlike", "drm: d"],
            )
            backfill_patch_series()
            writer.submit(
                WriteOp(label="ingest-like", fn=lambda conn: rechain_keys(conn, {key}))
            ).result(timeout=10)
        return real_submit(writer, payloads, after)

    monkeypatch.setattr(pending, "_submit_series_heads_batch", ingest_lands_first)
    backfill_series_chains()

    assert calls["n"] >= 1
    with seeded_db() as s:
        x_id = s.execute(select(Article.id).where(Article.message_id == "x@x")).scalar()
    heads = _heads(seeded_db)
    assert heads["x@x"] == x_id  # precondition: the 2024 posting split off
    assert heads["new@x"] == heads["new@x-p1"] == heads["new@x-p2"] == x_id


def test_a_run_started_without_reprocess_resumes_with_it(
    seeded_db, broker_active, monkeypatch
):
    """`--reprocess` changes nothing here, so it must not define the run
    either: an interrupted run resumes whichever way it is invoked
    again, instead of a second run walking from the newest article."""
    _split_corpus(seeded_db)
    _small_batches(monkeypatch)

    limited = _invoke(backfill_series_chains_command, "--limit", "4")
    assert limited.exit_code == 0, limited.output
    assert _runs(seeded_db)[-1][4] is False  # precondition: unfinished

    rest = _invoke(backfill_series_chains_command, "--reprocess")
    assert rest.exit_code == 0, rest.output
    assert _resumed_at(rest.output) is not None
    runs = [r for r in _runs(seeded_db) if r[0] == "backfill_series_chains"]
    assert len(runs) == 1 and runs[0][4] is True


def test_a_failed_batch_writes_none_of_its_key_s_heads(
    seeded_db, broker_active, monkeypatch
):
    _split_corpus(seeded_db)
    _small_batches(monkeypatch)
    # The first batch holds the newest cover, so it carries that key's
    # writes (its split cover and the cover's patch). Poisoning it, not a
    # later empty one, is what exercises the rollback.
    calls = _fail_batch(monkeypatch, "_submit_series_heads_batch", on_call=1)

    interrupted = _invoke(backfill_series_chains_command)

    assert interrupted.exit_code != 0, interrupted.output
    assert calls["n"] == 1
    assert _runs(seeded_db)[-1][4] is False
    assert _split_count(seeded_db) == []

    # Only the first call was poisoned, so the rerun resumes and completes.
    resumed = _invoke(backfill_series_chains_command)
    assert resumed.exit_code == 0, resumed.output
    assert len(_split_count(seeded_db)) == 6


def test_the_reprocess_flag_changes_nothing(seeded_db, broker_active):
    _split_corpus(seeded_db)
    assert _invoke(backfill_series_chains_command, "--reprocess").exit_code == 0
    first = _heads(seeded_db)
    assert _invoke(backfill_series_chains_command, "--reprocess").exit_code == 0
    assert _heads(seeded_db) == first
