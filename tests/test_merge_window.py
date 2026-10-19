"""`/merge-window` (#672, #703): linux-next minus Linus, by subsystem.

One test per state dimension in the spec: where a patch is (linux-next
only, also in a subsystem tree, landed under the same or another sha),
whether it is archived, series shape, canonical list, empty tree. Plus
the validator (both walk timestamps move the ETag) and the one-spelling
rule for the page's URL (sitemap `loc` equals the page canonical).
"""

import os
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from mimir.merge_window import (
    MERGE_WINDOW_PATH,
    NO_SUBSYSTEM,
    linus_tag,
    merge_window,
    version_tags,
)
from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    Inbox,
    MainlineCommit,
    MainlineState,
)
from mimir.web.filters import _display_name_filter

_T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _inbox(s, name):
    return s.execute(select(Inbox).where(Inbox.name == name)).scalar_one()


def _article(
    s, msgid, *, inbox="alpha", days=0, key=None, version=None, pos=None, files=()
):
    art = Article(
        message_id=msgid,
        subject=f"[PATCH] {msgid}",
        author="Ann Author <ann@example.com>",
        date=_T0 + timedelta(days=days),
        patch_series_key=key,
        patch_series_version=version,
        patch_series_position=pos,
        lists=[
            ArticleList(
                inbox_id=_inbox(s, inbox).id, epoch="0.git", commit_sha="f" * 40
            )
        ],
        files=[ArticleFile(path=f) for f in files],
    )
    s.add(art)
    s.flush()
    return art


def _commit(s, msgid, tree, sha=None):
    s.add(
        MainlineCommit(
            commit_sha=sha or (msgid.split("@")[0] + tree).ljust(40, "0")[:40],
            message_id=msgid,
            tree_name=tree,
            committed_at=_T0,
        )
    )


def _groups(mw):
    return [*mw.sections, *mw.smaller]


def _ids(mw):
    """{subsystem: [(subject, patches, trees)]} for compact assertions."""
    return {
        g.name: [(r.subject, r.patches, r.trees) for r in g.rows] for g in _groups(mw)
    }


def _subsystem(s, name, files):
    from mimir.models import Subsystem, SubsystemPath

    sub = Subsystem(name=name, status="Maintained")
    sub.paths = [SubsystemPath(glob=f, is_exclude=False) for f in files]
    s.add(sub)


def test_queued_is_linux_next_minus_linus_by_message_id(seeded_db):
    """Only in linux-next: listed. Also in Linus, under the same or a
    different sha: landed, so not listed. The anti-join is on
    message-id because linux-next re-shas commits on every rebuild."""
    with seeded_db() as s:
        _article(s, "queued@x")
        _article(s, "landed-same@x")
        _article(s, "landed-other-sha@x")
        _commit(s, "queued@x", "linux-next")
        _commit(s, "landed-same@x", "linux-next", sha="a" * 40)
        _commit(s, "landed-same@x", "linus", sha="a" * 40)
        _commit(s, "landed-other-sha@x", "linux-next", sha="b" * 40)
        _commit(s, "landed-other-sha@x", "linus", sha="c" * 40)
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {NO_SUBSYSTEM: [("[PATCH] queued@x", 1, [])]}
    assert mw.queued == 1


def test_queued_patch_names_the_other_trees_carrying_it(seeded_db):
    with seeded_db() as s:
        _article(s, "nn@x")
        _commit(s, "nn@x", "linux-next")
        _commit(s, "nn@x", "net-next")
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {NO_SUBSYSTEM: [("[PATCH] nn@x", 1, ["net-next"])]}


def test_unarchived_queued_messages_are_counted_not_listed(seeded_db):
    with seeded_db() as s:
        _article(s, "here@x")
        _commit(s, "here@x", "linux-next")
        _commit(s, "elsewhere@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert mw.queued == 2
    assert mw.not_archived == 1
    assert [r.subject for g in _groups(mw) for r in g.rows] == ["[PATCH] here@x"]


def test_series_with_cover_is_one_row_named_by_the_cover(seeded_db):
    """The cover itself is never committed, so it is found by series
    key and version, not among the queued rows."""
    with seeded_db() as s:
        cover = _article(s, "cover@x", key="k", version="v2", pos=0)
        cover.subject = "[PATCH v2 0/2] net: the series"
        for n in (1, 2):
            _article(s, f"p{n}@x", key="k", version="v2", pos=n, days=n)
            _commit(s, f"p{n}@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {NO_SUBSYSTEM: [("[PATCH v2 0/2] net: the series", 2, [])]}
    assert _groups(mw)[0].rows[0].url.endswith(f"/{cover.id}")


def test_series_without_archived_cover_is_named_by_its_oldest_patch(seeded_db):
    with seeded_db() as s:
        for n in (2, 1):
            _article(s, f"q{n}@x", key="nocover", version="v1", pos=n, days=n)
            _commit(s, f"q{n}@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {NO_SUBSYSTEM: [("[PATCH] q1@x", 2, [])]}


def test_sections_largest_first_and_rows_newest_first(seeded_db, monkeypatch):
    import mimir.merge_window as mwmod

    monkeypatch.setattr(mwmod, "SECTION_MIN_PATCHES", 1)
    with seeded_db() as s:
        _subsystem(s, "MM", ["mm/"])
        _subsystem(s, "NET", ["net/"])
        for n, days in (("old", 1), ("new", 5)):
            _article(s, f"{n}@x", days=days, files=[f"net/{n}.c"])
            _commit(s, f"{n}@x", "linux-next")
        _article(s, "lone@x", files=["mm/lone.c"])
        _commit(s, "lone@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert [g.name for g in mw.sections] == ["NET", "MM"]
    assert [r.subject for r in mw.sections[0].rows] == [
        "[PATCH] new@x",
        "[PATCH] old@x",
    ]


def test_primary_is_the_most_specific_subsystem_never_the_catch_all(seeded_db):
    """A patch in mlx5 goes under MLX5, not the broader NETWORKING
    DRIVERS it also matches; the catch-all never groups anything, so a
    patch only it matches lands in "(no subsystem)"."""
    with seeded_db() as s:
        _subsystem(s, "THE REST", ["*", "*/"])
        _subsystem(s, "NETWORKING DRIVERS", ["drivers/net/"])
        _subsystem(s, "MLX5", ["drivers/net/ethernet/mellanox/mlx5/"])
        _article(s, "mlx@x", files=["drivers/net/ethernet/mellanox/mlx5/en.c"])
        _commit(s, "mlx@x", "linux-next")
        _article(s, "misc@x", files=["scripts/checkpatch.pl"])
        _commit(s, "misc@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    by_name = {g.name: g for g in _groups(mw)}
    assert set(by_name) == {"MLX5", NO_SUBSYSTEM}
    assert by_name["MLX5"].rows[0].also == ["NETWORKING DRIVERS"]
    assert by_name[NO_SUBSYSTEM].rows[0].subject == "[PATCH] misc@x"


def test_series_is_grouped_once_by_all_its_files(seeded_db):
    """Patch 1 touches only the broad subsystem, patch 2 the specific
    one: the series appears once, under the specific one."""
    with seeded_db() as s:
        _subsystem(s, "NETWORKING DRIVERS", ["drivers/net/"])
        _subsystem(s, "MLX5", ["drivers/net/ethernet/mellanox/mlx5/"])
        for n, path in (
            (1, "drivers/net/core.c"),
            (2, "drivers/net/ethernet/mellanox/mlx5/en.c"),
        ):
            _article(s, f"s{n}@x", key="k", version="v1", pos=n, days=n, files=[path])
            _commit(s, f"s{n}@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {"MLX5": [("[PATCH] s1@x", 2, [])]}


def _series(s, files_per_patch):
    for n, files in enumerate(files_per_patch, 1):
        _article(s, f"s{n}@x", key="k", version="v1", pos=n, days=n, files=files)
        _commit(s, f"s{n}@x", "linux-next")


def test_tree_wide_series_is_not_decided_by_one_deep_driver_file(seeded_db):
    """One file in a deeply nested driver must not file a series that
    touches many drivers under its parent (#786)."""
    with seeded_db() as s:
        _subsystem(s, "DRM DRIVERS", ["drivers/gpu/drm/"])
        _subsystem(s, "ARM KOMEDA", ["drivers/gpu/drm/arm/display/komeda/"])
        _series(
            s,
            [
                ["drivers/gpu/drm/amd/a.c", "drivers/gpu/drm/i915/b.c"],
                ["drivers/gpu/drm/nouveau/c.c", "drivers/gpu/drm/msm/d.c"],
                ["drivers/gpu/drm/arm/display/komeda/e.c"],
            ],
        )
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {"DRM DRIVERS": [("[PATCH] s1@x", 3, [])]}
    assert _groups(mw)[0].rows[0].also == ["ARM KOMEDA"]


def test_driver_patch_with_one_shared_header_stays_on_its_driver(seeded_db):
    with seeded_db() as s:
        _subsystem(s, "FOO DRIVER", ["drivers/gpu/drm/foo/"])
        _subsystem(s, "DRM DRIVERS", ["drivers/gpu/drm/", "include/drm/"])
        _series(
            s,
            [
                [
                    "drivers/gpu/drm/foo/a.c",
                    "drivers/gpu/drm/foo/b.c",
                    "drivers/gpu/drm/foo/c.c",
                    "include/drm/h.h",
                ]
            ],
        )
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {"FOO DRIVER": [("[PATCH] s1@x", 1, [])]}
    assert _groups(mw)[0].rows[0].also == ["DRM DRIVERS"]


def test_broad_subsystem_with_slightly_more_files_does_not_outvote_a_deeper_one(
    seeded_db,
):
    """A cross-tree series: defconfig churn in a broad subsystem covers
    4 files, the deeper JIT subsystem 3. Three is at least half of the
    best coverage, so the deeper one is still a candidate and wins."""
    with seeded_db() as s:
        _subsystem(s, "M68K", ["arch/m68k/"])
        _subsystem(s, "BPF JIT", ["arch/x86/net/"])
        _series(
            s,
            [
                [f"arch/m68k/configs/c{n}_defconfig" for n in range(4)],
                [f"arch/x86/net/j{n}.c" for n in range(3)],
            ],
        )
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {"BPF JIT": [("[PATCH] s1@x", 2, [])]}
    assert _groups(mw)[0].rows[0].also == ["M68K"]


def test_a_file_touched_by_many_patches_counts_once(seeded_db):
    """Coverage counts the posting's distinct files: three patches
    editing the same Komeda file are one file of three, not three of
    five, so DRM DRIVERS covers more than half and Komeda does not."""
    with seeded_db() as s:
        _subsystem(s, "DRM DRIVERS", ["drivers/gpu/drm/"])
        _subsystem(s, "ARM KOMEDA", ["drivers/gpu/drm/arm/display/komeda/"])
        komeda = "drivers/gpu/drm/arm/display/komeda/e.c"
        _series(
            s,
            [
                [komeda],
                [komeda],
                [komeda],
                ["drivers/gpu/drm/amd/a.c", "drivers/gpu/drm/i915/b.c"],
            ],
        )
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {"DRM DRIVERS": [("[PATCH] s1@x", 4, [])]}


def test_a_section_needs_twenty_five_patches(seeded_db):
    with seeded_db() as s:
        _subsystem(s, "BIG", ["big/"])
        _subsystem(s, "SMALL", ["small/"])
        for name, n in (("big", 25), ("small", 24)):
            for i in range(n):
                _article(s, f"{name}{i}@x", files=[f"{name}/{i}.c"])
                _commit(s, f"{name}{i}@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert [g.name for g in mw.sections] == ["BIG"]
    assert [g.name for g in mw.smaller] == ["SMALL"]
    assert mw.smaller_patches == 24


def test_null_canonical_falls_back_to_a_linked_list(seeded_db):
    """`canonical_inbox_id` NULL: the same fallback every page uses."""
    with seeded_db() as s:
        art = _article(s, "nocanon@x", inbox="beta")
        art.canonical_inbox_id = None
        _commit(s, "nocanon@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert [r.inbox_name for g in _groups(mw) for r in g.rows] == ["beta"]


def test_empty_linux_next_renders_the_empty_state(client):
    r = client.get(MERGE_WINDOW_PATH)
    assert r.status_code == 200
    assert "Nothing is queued" in r.get_data(as_text=True)


def test_page_lists_queued_patch_and_links_its_thread(client):
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        art = _article(s, "page@x")
        _commit(s, "page@x", "linux-next")
        _commit(s, "page@x", "net-next")
        s.commit()
        url = f"/alpha/{art.date:%Y}/{art.date:%m}/{art.id}"
    html = client.get(MERGE_WINDOW_PATH).get_data(as_text=True)
    assert f'<a href="{url}">' in html
    assert re.search(r"also in net-next\b", html)


def test_rules_version_moves_the_etag(client):
    """The grouping follows MAINTAINERS, so a rules rebuild (a new
    `last_commit_sha` for linus) must move the validator too."""
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        s.add(
            MainlineState(
                tree_name="linus", last_walked_at=_T0, last_commit_sha="a" * 40
            )
        )
        s.commit()
    before = client.get(MERGE_WINDOW_PATH).headers["ETag"]
    with SessionLocal() as s:
        s.get(MainlineState, "linus").last_commit_sha = "b" * 40
        s.commit()
    assert client.get(MERGE_WINDOW_PATH).headers["ETag"] != before


@pytest.mark.parametrize("tree", ["linux-next", "linus"])
def test_walk_timestamp_moves_the_etag(client, tree):
    """Each tree's finished walk must move the validator: the page is
    `public, no-cache`, so the ETag is all that stands between a new
    linux-next and every cache holding the old list."""
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        for name in ("linux-next", "linus"):
            s.add(MainlineState(tree_name=name, last_walked_at=_T0))
        s.commit()
    before = client.get(MERGE_WINDOW_PATH).headers["ETag"]
    with SessionLocal() as s:
        s.get(MainlineState, tree).last_walked_at = _T0 + timedelta(hours=1)
        s.commit()
    after = client.get(MERGE_WINDOW_PATH)
    assert after.headers["ETag"] != before
    assert (
        client.get(
            MERGE_WINDOW_PATH, headers={"If-None-Match": after.headers["ETag"]}
        ).status_code
        == 304
    )


def test_sitemap_loc_is_the_page_canonical(client):
    html = client.get(MERGE_WINDOW_PATH).get_data(as_text=True)
    canonical = re.search(r'<link rel="canonical" href="([^"]+)"', html).group(1)
    sitemap = client.get("/meta-sitemap.xml").get_data(as_text=True)
    locs = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    assert canonical.endswith(MERGE_WINDOW_PATH)  # precondition
    assert canonical in locs


def test_front_page_links_the_merge_window(client):
    html = client.get("/").get_data(as_text=True)
    assert f'href="{MERGE_WINDOW_PATH}"' in html


def _tagged_repo(tmp_path, tags):
    from dulwich.objects import Commit, Tree
    from dulwich.repo import Repo

    repo = Repo.init_bare(str(tmp_path / "linux.git"), mkdir=True)
    tree = Tree()
    repo.object_store.add_object(tree)
    commit = Commit()
    commit.tree = tree.id
    commit.author = commit.committer = b"L <l@x>"
    commit.author_time = commit.commit_time = 1700000000
    commit.author_timezone = commit.commit_timezone = 0
    commit.message = b"x"
    repo.object_store.add_object(commit)
    repo.refs[b"HEAD"] = commit.id
    for t in tags:
        repo.refs[b"refs/tags/" + t.encode()] = commit.id
    return tmp_path / "linux.git"


def test_linus_tag_is_the_highest_version(tmp_path):
    """Numeric, not string, order; a release after its rcs; non-version
    tags ignored."""
    path = _tagged_repo(
        tmp_path, ["v7.2", "v7.3-rc4", "v7.3-rc10", "v7.10-rc1", "v2.6.39", "junk"]
    )
    assert linus_tag(path) == "v7.10-rc1"


def test_version_tags_lowest_first(tmp_path):
    """Numeric order, each release after its rcs, so the tag before a
    release is its last rc (the pull request anchor while the window is
    open, #703)."""
    path = _tagged_repo(
        tmp_path, ["v7.3", "v7.3-rc10", "v7.2", "v7.3-rc9", "v2.6.39", "junk"]
    )
    assert version_tags(path) == ["v2.6.39", "v7.2", "v7.3-rc9", "v7.3-rc10", "v7.3"]
    assert version_tags(tmp_path / "absent.git") == []


def test_linus_tag_release_beats_its_candidates(tmp_path):
    assert linus_tag(_tagged_repo(tmp_path, ["v7.3-rc7", "v7.3"])) == "v7.3"


def test_linus_tag_missing_mirror_or_tags_is_none(tmp_path):
    assert linus_tag(tmp_path / "absent.git") is None
    assert linus_tag(_tagged_repo(tmp_path, ["junk"])) is None


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v7.3", "The merge window after v7.3 is open."),
        ("v7.4-rc3", "The next merge window opens when v7.4 is released."),
        (None, None),
    ],
)
def test_page_says_whether_the_merge_window_is_open(client, monkeypatch, tag, expected):
    """A release tag means the window after it is open; an rc tag means
    it opens at that release. No predicted version number (#703)."""
    import mimir.merge_window as mw

    monkeypatch.setattr(mw, "linus_tag", lambda path: tag)
    text = re.sub(r"<[^>]+>", "", client.get(MERGE_WINDOW_PATH).get_data(as_text=True))
    sentences = re.findall(
        r"The (?:next )?merge window .*?(?:is open|is released)\.", text
    )
    assert sentences == ([expected] if expected else [])


def test_long_section_folds_after_twenty_rows(client):
    """Each section shows its first 20 rows; the rest sit in a <details>,
    still in the HTML so crawlers see every link (#703). The index links
    to the section by its anchor."""
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        _subsystem(s, "KERNEL VIRTUAL MACHINE FOR X86 (KVM/x86)", ["arch/x86/kvm/"])
        for n in range(25):
            _article(s, f"fold{n:02d}@x", days=n, files=[f"arch/x86/kvm/{n}.c"])
            _commit(s, f"fold{n:02d}@x", "linux-next")
        s.commit()
    html = client.get(MERGE_WINDOW_PATH).get_data(as_text=True)
    anchor = "kernel-virtual-machine-for-x86-kvm-x86"
    assert f'<a href="#{anchor}">' in html
    section = html.split(f'<h2 id="{anchor}">', 1)[1].split("</h2>", 1)[1]
    section = section.split("<h2", 1)[0]
    visible, _, folded = section.partition("<details")
    assert visible.count("<li>") == 20
    assert folded.count("<li>") == 5
    assert re.search(r"<summary>Show all 25</summary>", folded)


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        (
            (14450, 9760),
            "9760 of 14450 commits in linux-next link their mailing-list patch",
        ),
        ((None, None), None),
    ],
)
def test_page_says_how_much_of_linux_next_it_can_see(client, counts, expected):
    """The share comes from the walk that built the rows on the page, so
    it always describes this snapshot (#703). Before the first walk that
    records it, the page keeps its general note."""
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        s.add(
            MainlineState(
                tree_name="linux-next",
                last_walked_at=_T0,
                walked_commits=counts[0],
                walked_linked=counts[1],
            )
        )
        _article(s, "cov@x")
        _commit(s, "cov@x", "linux-next")
        s.commit()
    text = re.sub(
        r"\s+",
        " ",
        re.sub(r"<[^>]+>", "", client.get(MERGE_WINDOW_PATH).get_data(as_text=True)),
    )
    found = re.search(
        r"\d+ of \d+ commits in linux-next link their mailing-list patch", text
    )
    assert (found.group(0) if found else None) == expected
    assert "Link: trailer" in text  # the general note stays either way


def test_site_base_change_moves_the_merge_window_etag(client, monkeypatch):
    """The page renders its canonical from `SITE_BASE_URL`, a knob that
    changes on a restart without a version bump, so it is part of the
    ETag, as on the message page (found in the 3.17.0 release review)."""
    from mimir import config

    monkeypatch.setattr(config.settings, "site_base_url", "https://old.example")
    first = client.get(MERGE_WINDOW_PATH)
    assert 'href="https://old.example/merge-window"' in first.get_data(as_text=True)

    monkeypatch.setattr(config.settings, "site_base_url", "https://new.example")
    fresh = client.get(MERGE_WINDOW_PATH)
    # Precondition: the body really did change.
    assert 'href="https://new.example/merge-window"' in fresh.get_data(as_text=True)

    revalidate = client.get(
        MERGE_WINDOW_PATH, headers={"If-None-Match": first.headers["ETag"]}
    )
    assert revalidate.status_code == 200


@pytest.mark.parametrize("knob", ["tree_display_name", "demoted_inboxes"])
def test_config_knobs_move_the_etag(client, monkeypatch, knob):
    """Restart-only config the page depends on is in its validator:
    tree labels ("also in <label>") and the demoted inboxes that decide
    which list a row links into."""
    from mimir.config import settings

    before = client.get(MERGE_WINDOW_PATH).headers["ETag"]
    if knob == "tree_display_name":
        monkeypatch.setattr(settings.trees["net-next"], "display_name", "netdev next")
    else:
        monkeypatch.setattr(
            settings,
            "canonical_demoted_inboxes",
            [*settings.canonical_demoted_inboxes, "alpha"],
        )
    assert client.get(MERGE_WINDOW_PATH).headers["ETag"] != before


@pytest.mark.parametrize("other", ["resend", "rfc"])
def test_unversioned_resend_and_rfc_are_one_posting_with_v1(seeded_db, other):
    """`resend` and `rfc` are revision 1, as `version_number_sql` rates
    them: the original and its repost are one row, named by the
    original cover (#691)."""
    with seeded_db() as s:
        cover = _article(s, "c1@x", key="k", version="v1", pos=0)
        cover.subject = "[PATCH 0/2] the series"
        _article(s, "c2@x", key="k", version=other, pos=0, days=5)
        for version, days in (("v1", 1), (other, 6)):
            for n in (1, 2):
                _article(
                    s,
                    f"{version}-{n}@x",
                    key="k",
                    version=version,
                    pos=n,
                    days=days + n,
                )
                _commit(s, f"{version}-{n}@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {NO_SUBSYSTEM: [("[PATCH 0/2] the series", 4, [])]}
    assert _groups(mw)[0].rows[0].url.endswith(f"/{cover.id}")


def test_different_revisions_are_two_postings(seeded_db):
    with seeded_db() as s:
        for version, days in (("v1", 1), ("v2", 5)):
            _article(s, f"{version}@x", key="k", version=version, pos=1, days=days)
            _commit(s, f"{version}@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert sorted(r.patches for g in _groups(mw) for r in g.rows) == [1, 1]


def test_rfc_before_v1_does_not_name_the_queued_posting(seeded_db):
    """The RFC is posted first, as usual on lkml, and only v1's patches
    are queued: a stored `vN` names the posting over `rfc`, whatever the
    archive order (#691)."""
    with seeded_db() as s:
        rfc = _article(s, "rfc0@x", key="k", version="rfc", pos=0)
        rfc.subject = "[RFC PATCH 0/2] the series"
        v1 = _article(s, "v10@x", key="k", version="v1", pos=0, days=10)
        v1.subject = "[PATCH 0/2] the series"
        for n in (1, 2):
            _article(s, f"rfc-{n}@x", key="k", version="rfc", pos=n, days=n)
            _article(s, f"v1-{n}@x", key="k", version="v1", pos=n, days=10 + n)
            _commit(s, f"v1-{n}@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {NO_SUBSYSTEM: [("[PATCH 0/2] the series", 2, [])]}
    assert _groups(mw)[0].rows[0].url.endswith(f"/{v1.id}")


def test_rfc_cover_does_not_name_queued_v1_without_its_cover(seeded_db):
    """v1's cover is not archived, the RFC's is, and only v1's patches
    are queued: the RFC cover must not name the posting (#691)."""
    with seeded_db() as s:
        rfc = _article(s, "rfc0@x", key="k", version="rfc", pos=0)
        rfc.subject = "[RFC PATCH 0/2] the series"
        for n in (1, 2):
            _article(s, f"rfc-{n}@x", key="k", version="rfc", pos=n, days=n)
            _article(s, f"v1-{n}@x", key="k", version="v1", pos=n, days=10 + n)
            _commit(s, f"v1-{n}@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {NO_SUBSYSTEM: [("[PATCH] v1-1@x", 2, [])]}


def test_queued_v1_patch_names_the_posting_over_an_older_queued_rfc_patch(seeded_db):
    """No cover archived: of the queued patches, the stored `vN` one
    names the posting even though the `rfc` one is older (#691)."""
    with seeded_db() as s:
        for msgid, version, days in (("rfc@x", "rfc", 1), ("v1@x", "v1", 5)):
            _article(s, msgid, key="k", version=version, pos=1, days=days)
            _commit(s, msgid, "linux-next")
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {NO_SUBSYSTEM: [("[PATCH] v1@x", 2, [])]}


def _next_repo(tmp_path, monkeypatch, commits):
    """A linux-next mirror holding `{sha: (author, parent shas)}`."""
    from dulwich.objects import Commit, Tree
    from dulwich.repo import Repo

    from mimir.config import settings

    repo = Repo.init_bare(str(tmp_path / "next.git"), mkdir=True)
    tree = Tree()
    repo.object_store.add_object(tree)
    made = {}
    for key, (author, parents) in commits.items():
        commit = Commit()
        commit.tree = tree.id
        commit.parents = [made[p] for p in parents]
        commit.author = commit.committer = author.encode()
        commit.author_time = commit.commit_time = 1700000000
        commit.author_timezone = commit.commit_timezone = 0
        commit.message = key.encode()
        repo.object_store.add_object(commit)
        made[key] = commit.id
    monkeypatch.setattr(settings.trees["linux-next"], "path", tmp_path / "next.git")
    return {k: v.decode() for k, v in made.items()}


def _authors(mw):
    return [r.author for g in _groups(mw) for r in g.rows]


def _shown(mw):
    return [_display_name_filter(a) for a in _authors(mw)]


def _bare_sender_page(seeded_db, sha):
    with seeded_db() as s:
        _article(s, "bare@x").author = "jane@example.com"
        _commit(s, "bare@x", "linux-next", sha=sha)
        s.commit()
        return merge_window(s)


def test_sender_without_a_name_gets_the_linux_next_commit_author(
    seeded_db, tmp_path, monkeypatch
):
    shas = _next_repo(tmp_path, monkeypatch, {"p": ("Jane Doe <jane@example.com>", [])})
    assert _shown(_bare_sender_page(seeded_db, shas["p"])) == ["Jane Doe"]


def test_a_merge_commit_does_not_name_the_series_author(
    seeded_db, tmp_path, monkeypatch
):
    """A cover's message-id links to the maintainer's merge commit."""
    shas = _next_repo(
        tmp_path,
        monkeypatch,
        {
            "a": ("Jane Doe <jane@example.com>", []),
            "b": ("Other <o@example.com>", []),
            "m": ("Maint Ainer <maint@example.com>", ["a", "b"]),
        },
    )
    with seeded_db() as s:
        _article(s, "cover@x", key="k", version="v1", pos=0).author = "jane@example.com"
        _commit(s, "cover@x", "linux-next", sha=shas["m"])
        s.commit()
        mw = merge_window(s)
    assert _authors(mw) == ["jane@example.com"]


def test_a_posting_takes_its_most_common_commit_author(
    seeded_db, tmp_path, monkeypatch
):
    shas = _next_repo(
        tmp_path,
        monkeypatch,
        {
            "a": ("Bob <bob@example.com>", []),
            "b": ("Jane Doe <jane@example.com>", []),
            "c": ("Jane Doe <jane@example.com>", []),
        },
    )
    with seeded_db() as s:
        for n, k in enumerate("abc"):
            art = _article(s, f"p{n}@x", key="k", version="v1", pos=n + 1)
            art.author = "jane@example.com"
            _commit(s, f"p{n}@x", "linux-next", sha=shas[k])
        s.commit()
        mw = merge_window(s)
    assert _shown(mw) == ["Jane Doe"]


def test_a_non_ascii_commit_author_name_renders_as_written(
    seeded_db, tmp_path, monkeypatch
):
    """The template shows `row.author|display_name`; kernel authors'
    names are often not ASCII."""
    shas = _next_repo(
        tmp_path, monkeypatch, {"p": ("Jürgen Groß <jgross@example.com>", [])}
    )
    assert _shown(_bare_sender_page(seeded_db, shas["p"])) == ["Jürgen Groß"]


def test_an_unreadable_mirror_keeps_the_page(seeded_db, tmp_path, monkeypatch):
    """An OSError reading the mirror (permissions, or a pack a background
    gc removed) leaves the sender as it was rather than failing the page."""
    shas = _next_repo(tmp_path, monkeypatch, {"p": ("Jane Doe <jane@example.com>", [])})
    objects = tmp_path / "next.git" / "objects"
    os.chmod(objects, 0)
    try:
        mw = _bare_sender_page(seeded_db, shas["p"])
    finally:
        os.chmod(objects, 0o755)
    assert _authors(mw) == ["jane@example.com"]


def test_a_corrupt_mirror_object_keeps_the_page(seeded_db, tmp_path, monkeypatch):
    shas = _next_repo(tmp_path, monkeypatch, {"p": ("Jane Doe <jane@example.com>", [])})
    path = tmp_path / "next.git" / "objects" / shas["p"][:2] / shas["p"][2:]
    os.chmod(path, 0o644)
    path.write_bytes(b"not zlib")
    assert _authors(_bare_sender_page(seeded_db, shas["p"])) == ["jane@example.com"]


def test_a_last_comma_first_commit_author_names_the_row(
    seeded_db, tmp_path, monkeypatch
):
    """A git ident is `name <email>`; a comma in the name is legal there."""
    shas = _next_repo(
        tmp_path, monkeypatch, {"p": ("Doe, Jane <jane@example.com>", [])}
    )
    assert _shown(_bare_sender_page(seeded_db, shas["p"])) == ["Doe, Jane"]


def test_a_commit_author_name_that_is_an_address_is_not_shown(
    seeded_db, tmp_path, monkeypatch
):
    shas = _next_repo(
        tmp_path, monkeypatch, {"p": ("j@example.com <j@example.com>", [])}
    )
    assert _authors(_bare_sender_page(seeded_db, shas["p"])) == ["jane@example.com"]


def test_a_tied_posting_takes_the_alphabetically_first_name(
    seeded_db, tmp_path, monkeypatch
):
    shas = _next_repo(
        tmp_path,
        monkeypatch,
        {"a": ("Zed <z@example.com>", []), "b": ("Ann <a@example.com>", [])},
    )
    with seeded_db() as s:
        for n, k in enumerate("ab"):
            _article(s, f"p{n}@x", key="k", version="v1", pos=n + 1).author = "j@x.org"
            _commit(s, f"p{n}@x", "linux-next", sha=shas[k])
        s.commit()
        mw = merge_window(s)
    assert _shown(mw) == ["Ann"]


def test_one_nameless_commit_does_not_erase_every_other_name(
    seeded_db, tmp_path, monkeypatch
):
    """A git ident with an empty name (`<addr>`) is one commit without a
    name, not a reason to drop the names of every other posting."""
    shas = _next_repo(
        tmp_path,
        monkeypatch,
        {
            "a": ("Jane Doe <jane@example.com>", []),
            "b": ("<anon@example.com>", []),
        },
    )
    with seeded_db() as s:
        _article(s, "a@x", days=1).author = "jane@example.com"
        _commit(s, "a@x", "linux-next", sha=shas["a"])
        _article(s, "b@x").author = "anon@example.com"
        _commit(s, "b@x", "linux-next", sha=shas["b"])
        s.commit()
        mw = merge_window(s)
    assert sorted(_shown(mw)) == ["Jane Doe", "unknown sender"]
