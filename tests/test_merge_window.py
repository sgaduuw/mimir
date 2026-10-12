"""`/merge-window` (#672): linux-next minus Linus, by mailing list.

One test per state dimension in the spec: where a patch is (linux-next
only, also in a subsystem tree, landed under the same or another sha),
whether it is archived, series shape, canonical list, empty tree. Plus
the validator (both walk timestamps move the ETag) and the one-spelling
rule for the page's URL (sitemap `loc` equals the page canonical).
"""

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from mimir.merge_window import MERGE_WINDOW_PATH, linus_tag, merge_window
from mimir.models import Article, ArticleList, Inbox, MainlineCommit, MainlineState

_T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _inbox(s, name):
    return s.execute(select(Inbox).where(Inbox.name == name)).scalar_one()


def _article(s, msgid, *, inbox="alpha", days=0, key=None, version=None, pos=None):
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


def _ids(mw):
    """{list: [(subject, patches, trees)]} for compact assertions."""
    return {
        lst.inbox_name: [(r.subject, r.patches, r.trees) for r in lst.rows]
        for lst in mw.lists
    }


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
    assert _ids(mw) == {"alpha": [("[PATCH] queued@x", 1, [])]}
    assert mw.queued == 1


def test_queued_patch_names_the_other_trees_carrying_it(seeded_db):
    with seeded_db() as s:
        _article(s, "nn@x")
        _commit(s, "nn@x", "linux-next")
        _commit(s, "nn@x", "net-next")
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {"alpha": [("[PATCH] nn@x", 1, ["net-next"])]}


def test_unarchived_queued_messages_are_counted_not_listed(seeded_db):
    with seeded_db() as s:
        _article(s, "here@x")
        _commit(s, "here@x", "linux-next")
        _commit(s, "elsewhere@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert mw.queued == 2
    assert mw.not_archived == 1
    assert [r.subject for lst in mw.lists for r in lst.rows] == ["[PATCH] here@x"]


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
    assert _ids(mw) == {"alpha": [("[PATCH v2 0/2] net: the series", 2, [])]}
    assert mw.lists[0].rows[0].url.endswith(f"/{cover.id}")


def test_series_without_archived_cover_is_named_by_its_oldest_patch(seeded_db):
    with seeded_db() as s:
        for n in (2, 1):
            _article(s, f"q{n}@x", key="nocover", version="v1", pos=n, days=n)
            _commit(s, f"q{n}@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert _ids(mw) == {"alpha": [("[PATCH] q1@x", 2, [])]}


def test_lists_largest_first_and_rows_newest_first(seeded_db):
    with seeded_db() as s:
        for n, days in (("old", 1), ("new", 5)):
            _article(s, f"{n}@x", inbox="beta", days=days)
            _commit(s, f"{n}@x", "linux-next")
        _article(s, "lone@x", inbox="alpha")
        _commit(s, "lone@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert [lst.inbox_name for lst in mw.lists] == ["beta", "alpha"]
    assert [r.subject for r in mw.lists[0].rows] == ["[PATCH] new@x", "[PATCH] old@x"]


def test_null_canonical_falls_back_to_a_linked_list(seeded_db):
    """`canonical_inbox_id` NULL: the same fallback every page uses."""
    with seeded_db() as s:
        art = _article(s, "nocanon@x", inbox="beta")
        art.canonical_inbox_id = None
        _commit(s, "nocanon@x", "linux-next")
        s.commit()
        mw = merge_window(s)
    assert [lst.inbox_name for lst in mw.lists] == ["beta"]


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


def test_long_list_folds_after_twenty_rows(client):
    """Each list shows its first 20 rows; the rest sit in a <details>,
    still in the HTML so crawlers see every link (#703)."""
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        for n in range(25):
            _article(s, f"fold{n:02d}@x", days=n)
            _commit(s, f"fold{n:02d}@x", "linux-next")
        s.commit()
    html = client.get(MERGE_WINDOW_PATH).get_data(as_text=True)
    section = html.split('<h2 id="alpha">', 1)[1].split("</h2>", 1)[1]
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
