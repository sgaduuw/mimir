"""A cover's version typo takes its patches' version (#870).

A cover that says v2 over patches that all say v3 is a typo in the
cover, so the posting is v3. The re-chain owns the version: ingest,
replay and `backfill-patch-series` store each subject's own, and the
`rechain_keys` that ends each of their transactions writes the resolved
one, the same way `backfill-series-chains` does.
"""

import pytest
from sqlalchemy import select

from mimir.broker._context import get_active_writer
from mimir.broker.writes import WriteOp
from mimir.models import Article, ArticleList, Inbox
from mimir.patch_series import backfill_patch_series, posting_version, series_key
from mimir.patch_series_chains import backfill_series_chains, rechain_keys
from tests.test_chain_reply_patches import AUTHOR, _chain, _msg, _series
from tests.test_ingest.test_series_rechain import T2026, _arrive, _heads

# --- The rule, on subjects ----------------------------------------------------


@pytest.mark.parametrize(
    "cover,patches,stored,expected",
    [
        (
            "[PATCH v2 0/3] t",
            ["[PATCH v3 1/3] a", "[PATCH v3 2/3] b", "[PATCH v3 3/3] c"],
            "v2",
            "v3",
        ),
        ("[PATCH v2 0/2] t", ["[PATCH v3 1/2] a", "[PATCH 2/2] b"], "v2", "v2"),
        ("[PATCH v2 0/2] t", ["[PATCH v3 1/2] a", "[PATCH v4 2/2] b"], "v2", "v2"),
        ("[PATCH v3 0/2] t", ["[PATCH v2 1/2] a", "[PATCH v2 2/2] b"], "v3", "v3"),
        # Unmeasured, so excluded on purpose: `[PATCH 0/N]` claims no version.
        ("[PATCH 0/2] t", ["[PATCH v2 1/2] a", "[PATCH v2 2/2] b"], "v1", "v1"),
        ("[PATCH v2 0/1] t", ["[PATCH v3 1/1] a"], "v2", "v3"),
        ("[RFC PATCH v2 0/2] t", ["[PATCH v3 1/2] a", "[PATCH v3 2/2] b"], "v2", "v3"),
        ("not a cover", ["[PATCH v3 1/2] a", "[PATCH v3 2/2] b"], "v2", "v2"),
        ("[PATCH v2 0/2] t", [], "v2", "v2"),
    ],
    ids=[
        "all_above",
        "one_unversioned",
        "disagree",
        "all_below",
        "cover_unversioned",
        "single_child",
        "rfc_cover",
        "unparseable_cover",
        "no_children",
    ],
)
def test_posting_version(cover, patches, stored, expected):
    assert posting_version(cover, patches, stored) == expected


def test_the_base_is_the_parsed_cover_not_the_stored_version():
    """What flips a posting back when a deviating child arrives: the
    stored v3 written earlier is not the base."""
    patches = ["[PATCH v3 1/3] a", "[PATCH 3/3] c"]
    assert posting_version("[PATCH v2 0/3] t", patches, "v3") == "v2"


# --- Ingest, the backfills and the re-chain ----------------------------------

KEY = series_key("t", AUTHOR)
IDS = ("ty@x", "ty-p1@x", "ty-p2@x")


def _cover(version="v2", total=3):
    return _msg("ty@x", f"[PATCH {version} 0/{total}] t", body=b"cover")


def _patch(n, version="v3", total=3):
    v = f"{version} " if version else ""
    return _msg(f"ty-p{n}@x", f"[PATCH {v}{n}/{total}] foo {n}", "ty@x")


def _typo_posting(seeded_db, tmp_path):
    """A v2 cover, then its v3 patches a minute later in another epoch."""
    _arrive(seeded_db, tmp_path, "0.git", T2026, [_cover()])
    _arrive(seeded_db, tmp_path, "1.git", T2026 + 60, [_patch(1), _patch(2)])


def _versions(seeded_db, *ids):
    return {m: row[1] for m, row in _series(seeded_db, *ids).items()}


def _planned():
    result = backfill_series_chains()
    return result.heads_planned, result.versions_planned


def test_patches_arriving_after_a_typo_cover_set_its_version(
    seeded_db, tmp_path, broker_active
):
    _typo_posting(seeded_db, tmp_path)
    assert _series(seeded_db, *IDS) == {
        "ty@x": (KEY, "v3", 0),
        "ty-p1@x": (KEY, "v3", 1),
        "ty-p2@x": (KEY, "v3", 2),
    }
    assert _planned() == (0, 0)


def test_a_cover_arriving_after_its_patches(seeded_db, tmp_path, broker_active):
    """Ingest leaves the patches orphaned; the plain run that links them
    re-chains the key, which sets the version."""
    _arrive(seeded_db, tmp_path, "0.git", T2026, [_patch(1), _patch(2)])
    _arrive(seeded_db, tmp_path, "1.git", T2026 + 60, [_cover()])
    assert _series(seeded_db, *IDS) == {
        "ty@x": (KEY, "v2", 0),
        "ty-p1@x": (None, None, 1),
        "ty-p2@x": (None, None, 2),
    }

    backfill_patch_series()

    assert _versions(seeded_db, *IDS) == dict.fromkeys(IDS, "v3")
    assert _planned() == (0, 0)


def test_a_late_patch_without_a_version_flips_the_posting_back(
    seeded_db, tmp_path, broker_active
):
    _typo_posting(seeded_db, tmp_path)
    assert _versions(seeded_db, *IDS) == dict.fromkeys(IDS, "v3")  # precondition

    _arrive(seeded_db, tmp_path, "2.git", T2026 + 120, [_patch(3, version=None)])

    ids = (*IDS, "ty-p3@x")
    assert _versions(seeded_db, *ids) == dict.fromkeys(ids, "v2")
    assert _planned() == (0, 0)


def test_a_chain_reply_patch_takes_the_resolved_version(
    seeded_db, tmp_path, broker_active
):
    """2/3 and 3/3 reach the cover only through the walk (#840)."""
    msgs = _chain("cr", "v3", ["a", "b", "c"])
    msgs[0] = _msg("cr@x", "[PATCH v2 0/3] generic title", body=b"cover")
    _arrive(seeded_db, tmp_path, "0.git", T2026, msgs)

    ids = ("cr@x", "cr-p1@x", "cr-p2@x", "cr-p3@x")
    assert _versions(seeded_db, *ids) == dict.fromkeys(ids, "v3")
    assert _planned() == (0, 0)


def test_ingest_and_both_backfills_agree(seeded_db, tmp_path, broker_active):
    def snapshot():
        with seeded_db() as s:
            return s.execute(
                select(
                    Article.message_id,
                    Article.patch_series_key,
                    Article.patch_series_version,
                    Article.patch_series_position,
                    Article.patch_series_head_id,
                )
                .where(Article.message_id.in_(IDS))
                .order_by(Article.message_id)
            ).all()

    _typo_posting(seeded_db, tmp_path)
    before = snapshot()
    assert {row[2] for row in before} == {"v3"}  # precondition

    backfill_patch_series(reprocess=True)
    assert _planned() == (0, 0)
    assert snapshot() == before


# --- Rows stored before the fix ------------------------------------------------

STALE_KEY = series_key("generic title", AUTHOR)
OLD_TITLES = ["net: fix the frobnicator", "net: add a knob"]
# Reworded so that no title matches the old posting's (word-set Jaccard
# below 0.6), so only the version can join the two postings.
NEW_TITLES = ["net: repair frobnicator handling", "net: introduce knob option"]


def _row(s, msgid, subject, position, when, *, version, key, parent=None, head=None):
    inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
    art = Article(
        message_id=msgid,
        subject=subject,
        author=AUTHOR,
        date=when,
        thread_parent=parent,
        subject_normalized=subject.lower(),
        canonical_inbox_id=inbox.id,
        lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="d" * 40)],
        patch_series_key=key,
        patch_series_version=version,
        patch_series_position=position,
        patch_series_head_id=head,
    )
    s.add(art)
    s.flush()
    return art.id


def _seed_split_key(seeded_db, new_patch_version):
    """An old v1 posting, and a month later a cover stored v1 over
    patches of `new_patch_version` with reworded titles, written as the
    re-chain before the fix left them: every row v1, the new posting
    split off under its own head."""
    from datetime import UTC, datetime

    old, new = datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 2, 1, tzinfo=UTC)
    with seeded_db() as s:
        _row(
            s,
            "o@x",
            "[PATCH v1 0/2] generic title",
            0,
            old,
            version="v1",
            key=STALE_KEY,
        )
        for n, title in enumerate(OLD_TITLES, 1):
            _row(
                s,
                f"o-p{n}@x",
                f"[PATCH v1 {n}/2] {title}",
                n,
                old,
                version="v1",
                key=STALE_KEY,
                parent="o@x",
            )
        head = _row(
            s,
            "n@x",
            "[PATCH v1 0/2] generic title",
            0,
            new,
            version="v1",
            key=STALE_KEY,
        )
        s.execute(
            Article.__table__.update()
            .where(Article.id == head)
            .values(patch_series_head_id=head)
        )
        for n, title in enumerate(NEW_TITLES, 1):
            _row(
                s,
                f"n-p{n}@x",
                f"[PATCH {new_patch_version} {n}/2] {title}",
                n,
                new,
                version="v1",
                key=STALE_KEY,
                parent="n@x",
                head=head,
            )
        s.commit()


def _rechain_on_the_writer(key):
    get_active_writer().submit(
        WriteOp(label="test:rechain", fn=lambda conn: rechain_keys(conn, {key}))
    ).result(timeout=10)


NEW_IDS = ("n@x", "n-p1@x", "n-p2@x")
OLD_IDS = ("o@x", "o-p1@x", "o-p2@x")


def test_a_resolved_version_up_joins_a_key_split_on_stored_versions(
    seeded_db, broker_active
):
    _seed_split_key(seeded_db, "v2")

    _rechain_on_the_writer(STALE_KEY)

    assert _versions(seeded_db, *NEW_IDS) == dict.fromkeys(NEW_IDS, "v2")
    assert _versions(seeded_db, *OLD_IDS) == dict.fromkeys(OLD_IDS, "v1")
    heads = _heads(seeded_db)
    assert {heads[m] for m in (*OLD_IDS, *NEW_IDS)} == {None}


def test_patches_of_the_cover_s_version_keep_the_split(seeded_db, broker_active):
    """The inverse, and the precondition of the test above: with the
    patches still v1, the reworded titles alone split the key."""
    _seed_split_key(seeded_db, "v1")

    _rechain_on_the_writer(STALE_KEY)

    assert _versions(seeded_db, *NEW_IDS) == dict.fromkeys(NEW_IDS, "v1")
    heads = _heads(seeded_db)
    assert {heads[m] for m in NEW_IDS} == {"n@x"}
    assert {heads[m] for m in OLD_IDS} == {None}


def test_a_key_of_one_cover_with_a_typo_is_written(seeded_db, broker_active):
    """A key of one cover has no heads to change, so before this the
    chain pass skipped it."""
    from datetime import UTC, datetime

    when = datetime(2024, 1, 1, tzinfo=UTC)
    key = series_key("solo title", AUTHOR)
    with seeded_db() as s:
        _row(s, "so@x", "[PATCH v2 0/2] solo title", 0, when, version="v2", key=key)
        for n in (1, 2):
            _row(
                s,
                f"so-p{n}@x",
                f"[PATCH v3 {n}/2] bar {n}",
                n,
                when,
                version="v2",
                key=key,
                parent="so@x",
            )
        s.commit()

    assert _planned() == (0, 3)

    ids = ("so@x", "so-p1@x", "so-p2@x")
    assert _versions(seeded_db, *ids) == dict.fromkeys(ids, "v3")
    assert _planned() == (0, 0)
