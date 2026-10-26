"""A cover's pill states what every patch of its posting reached, with a
count when they differ (#938): "Queued · 3 of 5 landed".

The posting is the cover's series and version string, the slot rule
(#861) counts a re-posted patch once, the cover's declared total is the
denominator, and a cover never names a tree because its patches may sit
in several.
"""

import re
from datetime import UTC, datetime

import pytest

from mimir.lifecycle_status import LifecycleStatus, _bulk_uncached
from mimir.models import ArticleFile, ArticleTrailer, MainlineCommit
from tests.test_lifecycle_status import _lkml, _seed_article

KEY = "ab" * 20
WHEN = datetime(2026, 5, 1, tzinfo=UTC)


def _cover(session, tag="a", version="v2", total=5, **kw):
    return _seed_article(
        session,
        f"{tag}-cover@x",
        subject=f"[PATCH {version} 0/{total}] t",
        patch_series_key=KEY,
        patch_series_version=version,
        patch_series_position=0,
        **kw,
    )


def _patch(session, tag, n, version="v2", total=5, suffix="", **kw):
    return _seed_article(
        session,
        f"{tag}-p{n}{suffix}@x",
        subject=f"[PATCH {version} {n}/{total}] p{n}",
        patch_series_key=KEY,
        patch_series_version=version,
        patch_series_position=n,
        **kw,
    )


def _commit(session, article, tree="linus"):
    session.add(
        MainlineCommit(
            commit_sha=f"{article.id:x}".rjust(40, "c"),
            message_id=article.message_id,
            tree_name=tree,
            committed_at=WHEN,
        )
    )
    session.commit()


def _review(session, article):
    session.add(
        ArticleTrailer(
            article_id=article.id,
            role="Reviewed-by",
            name="Rev",
            address="r@x",
            address_normalized="r@x",
        )
    )
    session.commit()


def _pill(session, cover):
    return _bulk_uncached(session, [cover.id], _lkml(session))[cover.id]


def _posting(session, trees, **kw):
    """A v2 cover and one patch per entry of `trees`: 'linus', a tree
    name, 'reviewed' or None. Returns (cover, patches)."""
    cover = _cover(session, **kw)
    patches = []
    for n, tree in enumerate(trees, 1):
        p = _patch(session, "a", n, total=kw.get("total", 5))
        if tree == "reviewed":
            _review(session, p)
        elif tree:
            _commit(session, p, tree)
        patches.append(p)
    return cover, patches


def test_every_patch_queued_and_some_landed_reads_queued_with_a_count(session):
    cover, _ = _posting(session, ["linus"] * 3 + ["net-next"] * 2)
    got = _pill(session, cover)
    assert got.state == LifecycleStatus.QUEUED
    assert got.rollup == "3 of 5 landed"
    assert got.pill_label == "QUEUED"


def test_every_patch_landed_reads_landed_with_no_count(session):
    cover, _ = _posting(session, ["linus"] * 5)
    got = _pill(session, cover)
    assert got.state == LifecycleStatus.LANDED
    assert got.rollup is None
    assert got.pill_label == "LANDED"


def test_a_cover_never_names_a_tree(session):
    """Patches in two trees: the label is plain, and no tree shows."""
    cover, patches = _posting(session, ["net-next"] * 3 + ["linux-next"] * 2)
    got = _pill(session, cover)
    assert got.state == LifecycleStatus.QUEUED  # precondition: all queued
    assert got.tree is None
    assert got.pill_label == "QUEUED"
    assert "NEXT" not in (got.pill_label + (got.tooltip or "")).upper()


def test_a_reposted_patch_counts_once(session):
    """Six patch rows, five slots. The re-post is no slot of its own, and
    the winner (lowest id, no diff anywhere) is the landed one."""
    cover, patches = _posting(session, ["linus"] * 4 + ["reviewed"])
    _patch(session, "a", 1, suffix="b")
    assert _count_rows(session) == 6  # precondition: a re-post exists
    got = _pill(session, cover)
    assert got.state == LifecycleStatus.REVIEWED
    assert got.rollup == "4 of 5 landed"


def test_the_copy_with_a_diff_is_the_one_counted(session):
    """The first copy of slot 1 has no diff and no landing; the re-post
    has both. The slot's patch is the re-post, so it counts as landed."""
    cover, patches = _posting(session, [None] + ["linus"] * 4)
    repost = _patch(session, "a", 1, suffix="b")
    session.add(ArticleFile(article_id=repost.id, path="fs/foo.c"))
    _commit(session, repost)
    got = _pill(session, cover)
    assert got.state == LifecycleStatus.LANDED
    assert got.rollup is None


def test_a_patch_missing_from_the_archive_counts_as_pending(session):
    """Declared total 5, four stored and all landed: the pill must not
    claim the posting landed."""
    cover, _ = _posting(session, ["linus"] * 4)
    got = _pill(session, cover)
    assert got.state == LifecycleStatus.PENDING
    assert got.rollup == "4 of 5 landed"
    assert got.pill_label == "PENDING"


def test_a_pending_cover_shows_progress(session):
    cover, _ = _posting(session, ["linus", "linus", None, None, None])
    got = _pill(session, cover)
    assert (got.state, got.rollup) == (LifecycleStatus.PENDING, "2 of 5 landed")


@pytest.mark.parametrize(
    ("trees", "state", "rollup"),
    [
        (["reviewed"] * 5, LifecycleStatus.REVIEWED, None),
        (
            ["reviewed"] * 3 + ["net-next"] * 2,
            LifecycleStatus.REVIEWED,
            "2 of 5 queued",
        ),
        ([None] * 5, LifecycleStatus.PENDING, None),
    ],
    ids=["reviewed", "reviewed-and-queued", "nothing"],
)
def test_the_count_names_the_highest_rung_above_the_state(
    session, trees, state, rollup
):
    cover, _ = _posting(session, trees)
    got = _pill(session, cover)
    assert (got.state, got.rollup) == (state, rollup)


@pytest.mark.parametrize("landed", [0, 2])
def test_a_superseded_cover_keeps_superseded_and_counts_landings_above_zero(
    session, landed
):
    cover, _ = _posting(session, ["linus"] * landed + [None] * (5 - landed))
    _cover(session, tag="b", version="v3")
    got = _pill(session, cover)
    assert got.state == LifecycleStatus.SUPERSEDED  # precondition
    assert got.rollup == ("2 of 5 landed" if landed else None)


def test_a_superseded_unlanded_patch_has_reached_nothing(session):
    """Patch 2 is queued, but a v3 of patch 2 exists with no v3 cover, so
    the cover is not superseded and the patch is. It counts for no rung."""
    cover, _ = _posting(session, ["net-next"] * 5)
    _patch(session, "b", 2, version="v3")
    assert _pill(session, cover).state != LifecycleStatus.SUPERSEDED  # precondition
    got = _pill(session, cover)
    assert (got.state, got.rollup) == (LifecycleStatus.PENDING, "4 of 5 queued")


def test_a_superseded_patch_that_landed_still_counts_as_landed(session):
    cover, _ = _posting(session, ["linus"] * 5)
    _patch(session, "b", 2, version="v3")
    assert _pill(session, cover).state == LifecycleStatus.LANDED


def test_a_cover_without_patches_keeps_its_own_pill(session):
    cover = _cover(session)
    _review(session, cover)
    got = _pill(session, cover)
    assert (got.state, got.rollup) == (LifecycleStatus.REVIEWED, None)


def test_a_patch_keeps_its_own_pill(session):
    """The roll-up is for covers: a landed patch of an unlanded posting
    is landed, with no count."""
    _, patches = _posting(session, ["linus", None, None, None, None])
    got = _pill(session, patches[0])
    assert (got.state, got.rollup) == (LifecycleStatus.LANDED, None)


def test_another_posting_of_the_series_is_not_counted(session):
    """v1 patches landed under the same key; the v2 cover's posting is
    the v2 string only, so its two landings are all it shows."""
    cover, _ = _posting(session, ["linus", "linus", None, None, None])
    for n in range(1, 6):
        _commit(session, _patch(session, "old", n, version="v1"))
    got = _pill(session, cover)
    assert (got.state, got.rollup) == (LifecycleStatus.PENDING, "2 of 5 landed")


def _count_rows(session):
    from sqlalchemy import func, select

    from mimir.models import Article

    return session.scalar(select(func.count()).where(Article.patch_series_position > 0))


# --- surfaces ----------------------------------------------------------------


def _seed_thread(tmp_path):
    """A cover `c@x` and two patches in one thread, all in alpha."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("c@x", None), ("p1@x", "c@x"), ("p2@x", "c@x")]
    )
    with SessionLocal() as s:
        for mid, pos in (("c@x", 0), ("p1@x", 1), ("p2@x", 2)):
            a = s.get(Article, seeded[mid][0])
            a.subject = f"[PATCH v2 {pos}/2] t"
            a.patch_series_key = KEY
            a.patch_series_version = "v2"
            a.patch_series_position = pos
        s.commit()
    return seeded


def _line(html):
    m = re.search(
        r'<p class="status-line"><small><strong>Status:</strong> (.*?)</small></p>',
        html,
        re.DOTALL,
    )
    return m and re.sub(r"\s+", " ", m.group(1)).strip()


def test_the_cover_pill_status_line_and_markdown_agree(client, tmp_path):
    from tests.test_routes._helpers import _seed_mainline_commit

    seeded = _seed_thread(tmp_path)
    _seed_mainline_commit("p1@x", "1" * 40, "linus", WHEN)
    _seed_mainline_commit("p2@x", "2" * 40, "net-next", WHEN)
    url = seeded["c@x"][1] + "/t"
    html = client.get(url).get_data(as_text=True)
    assert re.search(r'badge-lifecycle-queued"[^>]*>\s*QUEUED', html), html
    assert "1 of 2 landed" in html
    assert _line(html) == "Queued, 1 of 2 landed."
    md = client.get(url + ".md").get_data(as_text=True)
    assert re.search(r"^- Status: QUEUED.*1 of 2 landed", md, re.MULTILINE), md


def test_a_patch_landing_moves_the_covers_etag_and_pill(client, tmp_path):
    """AGENTS.md: the validator carries every input of the body. The cover
    is primed, then its last patch lands: the next conditional request is a
    200 under a new ETag showing the new pill."""
    from tests.test_routes._helpers import _seed_mainline_commit

    seeded = _seed_thread(tmp_path)
    _seed_mainline_commit("p1@x", "1" * 40, "linus", WHEN)
    _seed_mainline_commit("p2@x", "2" * 40, "net-next", WHEN)
    url = seeded["c@x"][1] + "/t"
    before = client.get(url)
    assert "1 of 2 landed" in before.get_data(as_text=True)  # precondition
    _seed_mainline_commit("p2@x", "3" * 40, "linus", WHEN)
    after = client.get(url, headers={"If-None-Match": before.headers["ETag"]})
    assert after.status_code == 200
    assert after.headers["ETag"] != before.headers["ETag"]
    body = after.get_data(as_text=True)
    assert re.search(r'badge-lifecycle-landed"[^>]*>\s*LANDED', body), body
    assert "of 2 landed" not in body


def test_a_newer_revision_of_one_patch_moves_the_covers_etag(client, tmp_path):
    """A v3 of patch 2 with no v3 cover flips that patch's `live`, so the
    roll-up changes: the cover's ETag, and the pill cached under it, must
    move with it, not stay on a 304 with the old pill."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from tests.test_routes._helpers import _seed_mainline_commit

    seeded = _seed_thread(tmp_path)
    _seed_mainline_commit("p1@x", "1" * 40, "net-next", WHEN)
    _seed_mainline_commit("p2@x", "2" * 40, "net-next", WHEN)
    url = seeded["c@x"][1] + "/t"
    before = client.get(url)
    html = before.get_data(as_text=True)
    assert re.search(r'badge-lifecycle-queued"[^>]*>\s*QUEUED', html)  # precondition
    with SessionLocal() as s:
        s.add(
            Article(
                message_id="p2-v3@x",
                subject="[PATCH v3 2/2] t",
                subject_normalized="t",
                date=datetime(2024, 1, 9, tzinfo=UTC),
                patch_series_key=KEY,
                patch_series_version="v3",
                patch_series_position=2,
            )
        )
        s.commit()
    after = client.get(url, headers={"If-None-Match": before.headers["ETag"]})
    assert after.status_code == 200
    assert after.headers["ETag"] != before.headers["ETag"]
    assert "1 of 2 queued" in after.get_data(as_text=True)


def test_a_covers_own_commit_is_not_paired_with_the_postings_state(
    client, tmp_path, session
):
    """A cover with a mainline row of its own and a rolled-up posting: the
    status line and the tooltip name no sha, because the cover's commit is
    not the posting's evidence."""
    from tests.test_routes._helpers import _seed_mainline_commit

    seeded = _seed_thread(tmp_path)
    own_sha, patch_sha = "a" * 40, "b" * 40
    _seed_mainline_commit("c@x", own_sha, "net-next", WHEN)  # the cover's own
    _seed_mainline_commit("p1@x", patch_sha, "net-next", WHEN)
    _seed_mainline_commit("p2@x", patch_sha[::-1], "net-next", WHEN)
    from sqlalchemy import select

    from mimir.models import Article

    cover_id = session.scalar(select(Article.id).where(Article.message_id == "c@x"))
    from mimir.models import Inbox

    alpha = session.scalar(select(Inbox.id).where(Inbox.name == "alpha"))
    got = _bulk_uncached(session, [cover_id], alpha)[cover_id]
    assert got.state == LifecycleStatus.QUEUED  # precondition: rolled up
    assert got.commit_sha is None and got.committed_at is None
    assert own_sha[:12] not in (got.tooltip or "")
    html = client.get(seeded["c@x"][1] + "/t").get_data(as_text=True)
    assert own_sha[:12] not in html
    assert _line(html) == "Queued."
