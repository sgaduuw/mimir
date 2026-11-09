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
from tests._helpers import add_commit

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
    from dulwich.repo import Repo

    repo = Repo.init_bare(str(tmp_path / "linux.git"), mkdir=True)
    commit = add_commit(repo, b"L <l@x>", [], b"x")
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


def _page(client):
    from tests._dom import parse

    return parse(client.get(MERGE_WINDOW_PATH).get_data(as_text=True))


def _seed_postings(s, sizes):
    """One single-patch posting per queued patch, `sizes` = {subsystem:
    patches}. Each subsystem owns a directory named after it."""
    for name, count in sizes.items():
        directory = name.split()[0].lower()
        _subsystem(s, name, [f"{directory}/"])
        for n in range(count):
            msgid = f"{directory}{n}@x"
            _article(s, msgid, days=n, files=[f"{directory}/{n}.c"])
            _commit(s, msgid, "linux-next")
    s.commit()


@pytest.mark.parametrize(
    ("tag", "pill", "steps", "current"),
    [
        (
            "v7.3",
            "Open since v7.3",
            ["v7.3 released", "Merge window", "rc1 closes the window"],
            "Merge window",
        ),
        (
            "v7.4-rc3",
            "Closed until v7.4 is released",
            ["v7.4-rc3", "v7.4 release", "Merge window opens"],
            "v7.4-rc3",
        ),
        (None, None, [], None),
    ],
)
def test_header_shows_the_state_and_the_cycle(
    client, monkeypatch, tag, pill, steps, current
):
    """The pill and the three-step strip come only from what the page
    already knows. The next version is never named (Linus picks major
    bumps by hand), so the strip says "rc1", not "v7.4-rc1"."""
    import mimir.merge_window as mwmod

    monkeypatch.setattr(mwmod, "linus_tag", lambda path: tag)
    page = _page(client)
    pills = page.find_all("span", cls="pill")
    assert [p.text() for p in pills] == ([pill] if pill else [])
    if pill:
        assert ("pill-maintained" in pills[0].classes) == (tag == "v7.3")
    strips = page.find_all("ol", cls="mw-cycle")
    assert len(strips) == (1 if steps else 0)
    if steps:
        items = strips[0].find_all("li")
        assert [li.find("span", cls="mw-step-name").text() for li in items] == steps
        marked = [i for i in items if i.attrs.get("aria-current") == "step"]
        assert [m.find("span", cls="mw-step-name").text() for m in marked] == [current]
    html = client.get(MERGE_WINDOW_PATH).get_data(as_text=True)
    assert re.search(r"v\d+\.\d+-rc1\b", html) is None


def test_the_queue_is_one_list_of_disclosures(client, monkeypatch):
    import mimir.merge_window as mwmod
    from mimir.extensions import SessionLocal

    monkeypatch.setattr(mwmod, "SECTION_MIN_PATCHES", 2)
    with SessionLocal() as s:
        _seed_postings(
            s, {"KERNEL FOO (BAR)": 3, "MIDDLE": 2, "TINYONE": 1, "TINYTWO": 1}
        )
    page = _page(client)
    assert page.find("h2", id="queued").text() == "7 patches queued in linux-next"
    listing = page.find("ul", id="mw-subsystems")
    rows = [c for c in listing.children if getattr(c, "tag", None) == "li"]
    # Two sections, then the group of smaller subsystems.
    assert len(rows) == 3
    names = [r.find_all("span", cls="mw-name")[0].text() for r in rows]
    assert names == ["kernel foo (bar)", "middle", "2 smaller subsystems"]
    opened = ["open" in r.find_all("details")[0].attrs for r in rows]
    assert opened == [True, False, False]
    # Anchors survive: each section's <details> carries its fragment id.
    assert rows[0].find_all("details")[0].attrs["id"] == "kernel-foo-bar"
    group = rows[2].find_all("details")[0]
    assert group.attrs["id"] == "smaller-subsystems"
    nested = [
        li.find_all("span", cls="mw-name")[0].text()
        for li in group.find_all("li")
        if "data-filter-item" in li.attrs
    ]
    assert sorted(nested) == ["tinyone", "tinytwo"]
    # The count column and the bars agree with the data.
    counts = [r.find_all("span", cls="count")[0].text() for r in rows]
    assert counts == ["3", "2", "2"]
    widths = [r.find_all("rect")[0].attrs["width"] for r in rows[:2]]
    assert widths == ["100.0", "66.7"]
    # Every queued posting is still a link (crawlers see the closed rows).
    links = [a for a in page.find_all("a") if a.attrs["href"].startswith("/alpha/")]
    assert len(links) == 7
    # No fold boxes any more.
    html = client.get(MERGE_WINDOW_PATH).get_data(as_text=True)
    assert "data-fold" not in html
    assert "thread-toggle" not in html
    assert "fold-box" not in html


def test_list_tools_script_is_versioned(client, monkeypatch):
    """Static files are cached for a day, so the script URL carries the
    release (`?v=`), as `series-diff.js` does; an edit to it must not be
    served stale."""
    from urllib.parse import parse_qs, urlsplit

    from mimir import __version__

    sources = [
        s.attrs["src"]
        for s in _page(client).find_all("script")
        if "src" in s.attrs and s.attrs["src"].split("?")[0].endswith("/list-tools.js")
    ]
    assert len(sources) == 1
    assert parse_qs(urlsplit(sources[0]).query) == {"v": [__version__]}


def test_the_filter_ships_hidden_and_points_at_the_list(client, monkeypatch):
    import mimir.merge_window as mwmod
    from mimir.extensions import SessionLocal

    monkeypatch.setattr(mwmod, "SECTION_MIN_PATCHES", 1)
    with SessionLocal() as s:
        _seed_postings(s, {"KERNEL FOO": 1})
    page = _page(client)
    controls = page.find("div", cls="mw-find")
    assert "hidden" in controls.attrs
    box = controls.find("input")
    assert box.attrs["data-filter-input"] == "mw-subsystems"
    assert page.find("ul", id="mw-subsystems")
    scripts = [
        s.attrs["src"].split("?")[0]
        for s in page.find_all("script")
        if "src" in s.attrs
    ]
    assert [x for x in scripts if x.endswith("/js/list-tools.js")]
    assert not [x for x in scripts if "fold-box" in x]


def test_the_counting_note_follows_the_list_and_is_linked(client, monkeypatch):
    import mimir.merge_window as mwmod
    from mimir.extensions import SessionLocal

    monkeypatch.setattr(mwmod, "SECTION_MIN_PATCHES", 1)
    with SessionLocal() as s:
        _seed_postings(s, {"KERNEL FOO": 2})
        s.add(
            MainlineState(
                tree_name="linux-next",
                last_walked_at=_T0,
                walked_commits=10,
                walked_linked=6,
            )
        )
        s.commit()
    html = client.get(MERGE_WINDOW_PATH).get_data(as_text=True)
    page = _page(client)
    assert any(a.attrs["href"] == "#how-counted" for a in page.find_all("a"))
    note = page.find("p", id="how-counted")
    assert html.index('id="how-counted"') > html.index('id="mw-subsystems"')
    text = note.text()
    for sentence in (
        "2 patches in linux-next are not in Linus's tree yet, grouped by MAINTAINERS subsystem: each series goes under the most specific subsystem that covers at least half as many of its files as the subsystem covering the most.",
        "6 of 10 commits in linux-next link their mailing-list patch; the rest cannot be shown here.",
        "Only commits that link their mailing-list message (a Link: trailer) are counted.",
        "A series shows once, by its cover letter.",
    ):
        assert sentence in text, sentence


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        (
            (14450, 9760),
            "9,760 of 14,450 commits in linux-next link their mailing-list patch",
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
        r"(?<![\d,])([\d,]+) of ([\d,]+) commits in linux-next link their mailing-list patch",
        text,
    )
    assert (found.group(0) if found else None) == expected
    if found:
        assert tuple(int(n.replace(",", "")) for n in found.groups()) == (
            counts[1],
            counts[0],
        )
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


def test_linux_next_mirror_path_moves_the_etag(
    client, seeded_db, tmp_path, monkeypatch
):
    """Commit author names come from the linux-next mirror (#787), so its
    path is restart-only config the page depends on and belongs in the
    page's state, like the Linus mirror's."""
    from mimir.config import settings

    shas = _next_repo(tmp_path, monkeypatch, {"p": ("Jane Doe <jane@example.com>", [])})
    with seeded_db() as s:
        _article(s, "bare@x").author = "jane@example.com"
        _commit(s, "bare@x", "linux-next", sha=shas["p"])
        s.commit()
    monkeypatch.setattr(settings.trees["linux-next"], "path", tmp_path / "missing.git")
    first = client.get(MERGE_WINDOW_PATH)
    assert "Jane Doe" not in first.get_data(as_text=True)
    monkeypatch.setattr(settings.trees["linux-next"], "path", tmp_path / "next.git")
    again = client.get(
        MERGE_WINDOW_PATH, headers={"If-None-Match": first.headers["ETag"]}
    )
    assert again.status_code == 200
    assert "Jane Doe" in again.get_data(as_text=True)


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


def test_two_series_under_one_key_are_two_postings(seeded_db):
    """#816: one key, one revision number, two unrelated series told
    apart by the head. Each is its own row, named by its own cover."""
    with seeded_db() as s:
        for tag, head, days in (("a", None, 1), ("b", 77, 10)):
            cover = _article(s, f"{tag}0@x", key="k", version="v1", pos=0, days=days)
            cover.subject = f"[PATCH 0/1] series {tag}"
            patch = _article(s, f"{tag}1@x", key="k", version="v1", pos=1, days=days)
            for art in (cover, patch):
                art.patch_series_head_id = head
            _commit(s, f"{tag}1@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {
        NO_SUBSYSTEM: [("[PATCH 0/1] series b", 1, []), ("[PATCH 0/1] series a", 1, [])]
    }


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
    from dulwich.repo import Repo

    from mimir.config import settings

    repo = Repo.init_bare(str(tmp_path / "next.git"), mkdir=True)
    made = {}
    for key, (author, parents) in commits.items():
        made[key] = add_commit(
            repo, author.encode(), [made[p] for p in parents], key.encode()
        ).id
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


@pytest.mark.parametrize(
    ("author", "shown"),
    [
        ('"s@private.example" <s@private.example>', "Jane Doe"),
        ('"a@x.org via list" <list@y.org>', "Jane Doe"),
        ("Sam Smith <s@private.example>", "Sam Smith"),
    ],
)
def test_only_a_usable_sender_name_beats_the_commit_author(
    seeded_db, tmp_path, monkeypatch, author, shown
):
    shas = _next_repo(tmp_path, monkeypatch, {"p": ("Jane Doe <jane@example.com>", [])})
    with seeded_db() as s:
        _article(s, "sender@x").author = author
        _commit(s, "sender@x", "linux-next", sha=shas["p"])
        s.commit()
        mw = merge_window(s)
    assert _shown(mw) == [shown]
    assert "@" not in "".join(_shown(mw))


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


def _bar_widths(client):
    from tests._dom import parse

    page = parse(client.get(MERGE_WINDOW_PATH).get_data(as_text=True))
    return {
        li.attrs["data-filter-text"]: li.find("rect").attrs["width"]
        for li in page.find_all("li")
        if li.attrs.get("data-filter-text") is not None
    }


def test_smaller_subsystem_bars_share_the_largest_scale(client):
    """One scale for every bar on the page (#1000: "proportional to the
    largest"): a 3-patch subsystem must not draw the same bar as a
    30-patch one just because it sits in the smaller group."""
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        _seed_postings(s, {"ALPHA SUB": 30, "GAMMA": 3, "DELTA": 1})
    assert _bar_widths(client) == {
        "alpha sub": "100.0",
        "gamma": "10.0",
        "delta": "3.3",
    }


def test_the_no_subsystem_bucket_can_set_the_scale(client):
    """`(no subsystem)` always lands in the smaller group, even when it is
    the largest bucket. The scale is the largest of both lists, so no bar is
    wider than its box (against the section's own maximum it drew 154%)."""
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        _seed_postings(s, {"NAMED": 26})
        for n in range(40):
            _article(s, f"loose{n}@x", days=n)
            _commit(s, f"loose{n}@x", "linux-next")
        s.commit()
    widths = _bar_widths(client)
    assert widths == {"(no subsystem)": "100.0", "named": "65.0"}
    assert all(float(w) <= 100 for w in widths.values())


def _seed_for_the_pinned_section(s):
    """Every shape the section renders: a section with a multi-patch
    series that touches a second subsystem, a patch also in another
    tree, a smaller subsystem, and the no-subsystem bucket."""
    _subsystem(s, "KERNEL FOO (BAR)", ["foo/"])
    _subsystem(s, "FOO HELPERS", ["foo/helpers/"])
    _subsystem(s, "TINY", ["tiny/"])
    _article(s, "cover@x", key="k", version="v2", pos=0, days=3)
    for n in range(2):
        _article(
            s,
            f"series{n}@x",
            key="k",
            version="v2",
            pos=n + 1,
            days=3,
            files=[f"foo/a{n}.c", "foo/helpers/h.c"],
        )
        _commit(s, f"series{n}@x", "linux-next")
    _article(s, "single@x", days=5, files=["foo/s.c"])
    _commit(s, "single@x", "linux-next")
    _commit(s, "single@x", "net-next")
    _article(s, "tiny@x", days=1, files=["tiny/t.c"])
    _commit(s, "tiny@x", "linux-next")
    _article(s, "loose@x", days=2)
    _commit(s, "loose@x", "linux-next")
    s.commit()


def _live_section_html(client):
    html = client.get(MERGE_WINDOW_PATH).get_data(as_text=True)
    start = html.index('<h2 id="queued">')
    end = html.index('<p id="how-counted">')
    return " ".join(html[start:end].split())


PINNED_LIVE_SECTION = (
    '<h2 id="queued">5 patches queued in linux-next</h2> '
    '<p>Not in Linus\'s tree yet, grouped by MAINTAINERS subsystem. <a href="#how-counted">How these are counted</a></p> '
    '<div class="mw-find" data-filter-controls hidden> '
    '<div class="search-field"> '
    '<label for="mw-find-input">Find a subsystem</label> '
    '<input id="mw-find-input" type="search" data-filter-input="mw-subsystems" autocomplete="off"> '
    "</div></div> "
    '<ul class="mw-subsystems" id="mw-subsystems"> '
    '<li data-filter-item data-filter-text="kernel foo (bar)"> '
    '<details id="kernel-foo-bar" open> '
    "<summary> "
    '<span class="mw-name">kernel foo (bar)</span> '
    '<svg class="bar-meter" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true"><rect class="bar-added" width="100.0" height="6"/></svg> '
    '<span class="count">3</span> '
    "</summary> "
    '<ul class="divided"> '
    "<li> "
    '<a href="/alpha/2026/09/8">[PATCH] single@x</a> '
    "<br><small>Ann Author &middot; 2026-09-06 &middot; alpha &middot; also in net-next</small> "
    "</li> "
    "<li> "
    '<a href="/alpha/2026/09/5">[PATCH] cover@x</a> '
    "<small>2 patches</small> "
    "<br><small>Ann Author &middot; 2026-09-04 &middot; alpha &middot; touches foo helpers</small> "
    "</li> "
    "</ul> "
    "</details> "
    "</li> "
    "<li data-filter-item> "
    '<details id="smaller-subsystems"> '
    "<summary> "
    '<span class="mw-name">2 smaller subsystems</span> '
    '<span class="count">2</span> '
    "</summary> "
    "<p><small>Subsystems with fewer than 25 queued patches.</small></p> "
    '<ul class="mw-subsystems mw-subsystems-nested"> '
    '<li data-filter-item data-filter-text="tiny"> '
    '<details id="tiny"> '
    "<summary> "
    '<span class="mw-name">tiny</span> '
    '<svg class="bar-meter" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true"><rect class="bar-added" width="33.3" height="6"/></svg> '
    '<span class="count">1</span> '
    "</summary> "
    '<ul class="divided"> '
    "<li> "
    '<a href="/alpha/2026/09/9">[PATCH] tiny@x</a> '
    "<br><small>Ann Author &middot; 2026-09-02 &middot; alpha</small> "
    "</li> "
    "</ul> "
    "</details> "
    "</li> "
    '<li data-filter-item data-filter-text="(no subsystem)"> '
    '<details id="no-subsystem"> '
    "<summary> "
    '<span class="mw-name">(no subsystem)</span> '
    '<svg class="bar-meter" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true"><rect class="bar-added" width="33.3" height="6"/></svg> '
    '<span class="count">1</span> '
    "</summary> "
    '<ul class="divided"> '
    "<li> "
    '<a href="/alpha/2026/09/10">[PATCH] loose@x</a> '
    "<br><small>Ann Author &middot; 2026-09-03 &middot; alpha</small> "
    "</li> "
    "</ul> "
    "</details> "
    "</li> "
    "</ul> "
    "</details> "
    "</li> "
    "</ul>"
)


def test_the_live_section_renders_as_pinned(client, monkeypatch):
    """Pins the live `/merge-window` queue markup byte for byte (modulo
    whitespace), so extracting the grouping and the rendering into
    shared pieces (#1136) cannot change what the live page shows."""
    import mimir.merge_window as mwmod
    from mimir.extensions import SessionLocal

    monkeypatch.setattr(mwmod, "SECTION_MIN_PATCHES", 2)
    with SessionLocal() as s:
        _seed_for_the_pinned_section(s)
    assert _live_section_html(client) == PINNED_LIVE_SECTION


def test_a_release_history_groups_the_same_input_into_the_same_sections(
    seeded_db, monkeypatch
):
    """The queue and a release's recorded references are one grouping
    (#1136): the same message ids must give the same sections, the same
    smaller subsystems and the same counts on both pages."""
    import mimir.merge_window as mwmod
    from mimir.release_history import calculate_subsystems

    monkeypatch.setattr(mwmod, "SECTION_MIN_PATCHES", 2)
    with seeded_db() as s:
        _seed_for_the_pinned_section(s)
        live = merge_window(s)
        queued = [m for (m,) in s.execute(select(MainlineCommit.message_id)).all()]
        history = calculate_subsystems(s, queued)
    shape = [
        [(g.name, g.anchor, g.patches) for g in groups]
        for groups in (live.sections, live.smaller)
    ]
    # The precondition: the seed built a section, smaller subsystems
    # and the no-subsystem bucket, so the comparison is not of two empties.
    assert [name for name, *_ in shape[0]] == ["KERNEL FOO (BAR)"]
    assert [name for name, *_ in shape[1]] == ["TINY", NO_SUBSYSTEM]
    assert [
        [(g.name, g.anchor, g.patches) for g in groups]
        for groups in (history.sections, history.smaller)
    ] == shape
    assert history.smaller_patches == live.smaller_patches
    assert history.references == 5


def test_the_no_subsystem_bucket_never_gets_a_section_of_its_own(
    seeded_db, monkeypatch
):
    """However large, "(no subsystem)" stays in the smaller group, last,
    on the queue and on a release's history alike."""
    import mimir.merge_window as mwmod
    from mimir.release_history import calculate_subsystems

    monkeypatch.setattr(mwmod, "SECTION_MIN_PATCHES", 2)
    with seeded_db() as s:
        _subsystem(s, "NAMED", ["named/"])
        for n in range(3):
            _article(s, f"loose{n}@x", days=n)
            _commit(s, f"loose{n}@x", "linux-next")
        _article(s, "named@x", files=["named/a.c"])
        _commit(s, "named@x", "linux-next")
        s.commit()
        live = merge_window(s)
        history = calculate_subsystems(
            s, ["loose0@x", "loose1@x", "loose2@x", "named@x"]
        )
    for result in (live, history):
        assert [g.name for g in result.sections] == []
        assert [(g.name, g.patches) for g in result.smaller] == [
            ("NAMED", 1),
            (NO_SUBSYSTEM, 3),
        ]
