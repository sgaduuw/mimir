"""A cover letter's files are its patches' files (#844 Part 2).

A cover carries no diff, so it has no `article_files` rows of its own.
Its thread view and its message page used to show no subsystem line at
all, and the validator digested an empty path list. `article_paths`
answers for the posting (the cover's series and version), and the
subsystem line, the maintainer line, the series totals and the ETag all
read it.
"""

import re

from sqlalchemy import event

from mimir.models import Article, ArticleFile
from tests.test_routes._helpers import _metadata_url, _seed_subsystem, seed_thread_shape

KEY = "f0" * 20

# Message-id: (version, position, files). The cover and two patches of
# v2 are one thread; the v1 patch is the same series and position but an
# older posting, so its file must not count.
POSTING = {
    "cover@x": ("v2", 0, []),
    "p1@x": ("v2", 1, ["drivers/foo/a.c", "drivers/foo/b.c"]),
    "p2@x": ("v2", 2, ["drivers/foo/a.c", "fs/bar/c.c"]),
    "old@x": ("v1", 1, ["net/zzz/old.c"]),
}


def _seed(tmp_path):
    from mimir import maintainer_allowlist
    from mimir.extensions import SessionLocal

    _seed_subsystem(
        "FOO DRIVER",
        "Maintained",
        ["drivers/foo/"],
        [
            ("M", "Foo Maintainer", "foo.maint@example.org"),
            ("R", "Rita Reviewer", "rita.reviewer@example.org"),
        ],
    )
    # An address the profile route cannot serve: the name stays, unlinked.
    _seed_subsystem(
        "BAR FS", "Maintained", ["fs/bar/"], [("M", "Bar Keeper", "bar keeper@x")]
    )
    _seed_subsystem("ZZZ NET", "Maintained", ["net/zzz/"], [("M", "Zed", "zed@x.org")])
    maintainer_allowlist.invalidate()

    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [
            ("cover@x", None),
            ("p1@x", "cover@x"),
            ("p2@x", "cover@x"),
            ("re@x", "p1@x"),
            ("old@x", None),
        ],
    )
    with SessionLocal() as s:
        for mid, (version, position, files) in POSTING.items():
            a = s.get(Article, seeded[mid][0])
            a.subject = f"[PATCH {version} {position}/2] foo: bar"
            a.patch_series_key = KEY
            a.patch_series_version = version
            a.patch_series_position = position
            s.add_all(ArticleFile(article_id=a.id, path=p) for p in files)
        s.commit()
    return seeded


def _visible(html):
    return re.sub(r"<[^>]+>", "", html)


def test_cover_pages_show_the_posting_s_subsystems_and_maintainers(client, tmp_path):
    seeded = _seed(tmp_path)
    cover_url = seeded["cover@x"][1]
    for url in (cover_url + "/t", _metadata_url(cover_url)):
        html = client.get(url).get_data(as_text=True)
        visible = _visible(html)
        assert "foo driver" in visible and "bar fs" in visible, url
        # The v1 patch is another posting of the same series.
        assert "zzz net" not in visible, url
        assert 'href="/maintainers/foo.maint@example.org"' in html, url
        assert "Bar Keeper" in visible, url
        # M: only, and no address as text: the link is the only copy.
        assert "Rita Reviewer" not in visible, url
        for address in ("foo.maint@example.org", "rita.reviewer@", "bar keeper@x"):
            assert address not in visible, (url, address)
        assert "rita.reviewer@" not in html and "bar keeper@x" not in html, url


def test_the_markdown_thread_lists_names_and_no_address(client, tmp_path):
    seeded = _seed(tmp_path)
    md = client.get(seeded["cover@x"][1] + "/t.md").get_data(as_text=True)
    assert re.search(
        r"^- Maintainers: Bar Keeper, Foo Maintainer$", md, re.MULTILINE
    ), md
    for address in ("foo.maint@example.org", "rita.reviewer@", "bar keeper@x"):
        assert address not in md, address


def test_series_totals_count_the_posting_s_patches_and_distinct_files(client, tmp_path):
    """Two v2 patches, three distinct files: a.c is in both. The v1
    patch's file is another posting's."""
    seeded = _seed(tmp_path)
    html = client.get(seeded["cover@x"][1] + "/t").get_data(as_text=True)
    found = re.findall(r"(\d+) patch(?:es)?, (\d+) files?", _visible(html))
    assert found == [("2", "3")], found


def test_series_totals_are_not_shown_for_a_patch_root(client, tmp_path):
    """A single patch carries git's diffstat in its own body, so a total
    there would only copy lore. The v1 patch is its own thread's root."""
    seeded = _seed(tmp_path)
    html = client.get(seeded["old@x"][1] + "/t").get_data(as_text=True)
    assert "zzz net" in _visible(html)  # precondition: its own files still count
    assert not re.search(r"\d+ patch(?:es)? touching", _visible(html))


def test_a_posting_patch_gaining_a_path_moves_the_etag(client, tmp_path):
    """AGENTS.md: a validator carries every input the body depends on.
    The cover's subsystem line now reads its patches' files, so a path
    added to a patch must move the cover's ETag on both surfaces. Caches
    are primed by a normal request, not cleared."""
    from mimir.extensions import SessionLocal

    seeded = _seed(tmp_path)
    cover_url = seeded["cover@x"][1]
    urls = (cover_url + "/t", cover_url)
    before = {u: client.get(u) for u in urls}
    assert all(
        "zzz net" not in _visible(r.get_data(as_text=True)) for r in before.values()
    )

    with SessionLocal() as s:
        s.add(ArticleFile(article_id=seeded["p2@x"][0], path="net/zzz/new.c"))
        s.commit()

    for u in urls:
        etag = before[u].headers["ETag"]
        after = client.get(u, headers={"If-None-Match": etag})
        assert after.status_code == 200, f"{u}: served 304 after the posting changed"
        assert after.headers["ETag"] != etag, u
        assert "zzz net" in _visible(after.get_data(as_text=True)), u


def test_article_paths_reads_the_posting_by_the_series_key_index(seeded_db):
    """AGENTS.md check 6: the cover read runs on every render and every
    304 of a cover's pages. Production's rows for the two indexes it
    must use, read 2026-10-26."""
    from mimir.subsystems import article_paths
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    with seeded_db() as s:
        cover = Article(
            message_id="plan-cover@x",
            subject="[PATCH v2 0/1] plan",
            subject_normalized="plan",
            patch_series_key=KEY,
            patch_series_version="v2",
            patch_series_position=0,
        )
        s.add(cover)
        s.commit()
        cover_id = cover.id

    stats = (
        *PRODUCTION_STATS,
        ("articles", "ix_articles_patch_series_key", "17797373 66"),
        ("article_files", "sqlite_autoindex_article_files_1", "15194624 3 1"),
        ("article_files", "ix_article_files_path", "15194624 24"),
    )
    with planner_stats(seeded_db, stats), seeded_db() as s:
        cover = s.get(Article, cover_id)
        plan = plan_of(s, lambda: article_paths(s, cover), "article_files")
    assert "USING INDEX ix_articles_patch_series_key (patch_series_key=?)" in plan, plan
    assert re.search(
        r"article_files USING (COVERING )?INDEX sqlite_autoindex_article_files_1 "
        r"\(article_id=\?\)",
        plan,
    ), plan


# Statements the message page runs, counted 2026-10-26 on
# feature/844-revisions-inline 58fd0a5, before Part 2: per page, a full
# render with caches cleared and the 304 that revalidates it. The patch
# page renders the same subsystem line before and after, so its budget
# is +0: reading files through `article_paths` replaced the two reads
# it had. The cover's full render is not comparable, because it now has
# a subsystem line to render (the rules index and the allowlist load on
# a cold cache), but its 304 renders nothing and stays +0 too. The
# patch page's 47 is since #844 Part 3, which dropped the card's own
# landing query (the status line reads the pill's row). #1003 dropped the
# canonical's "does the thread have another member" lookup: -1 on every
# count.
MESSAGE_PAGE_STATEMENTS = {"p1@x": (46, 20), "cover@x": (None, 18)}


def test_message_page_statement_budget(client, tmp_path):
    from sqlalchemy import delete

    from mimir.extensions import SessionLocal, engine
    from mimir.models import CacheEntry

    seeded = _seed(tmp_path)
    statements: list[str] = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    counted = {}
    event.listen(engine, "before_cursor_execute", grab)
    try:
        for mid, (full_budget, _) in MESSAGE_PAGE_STATEMENTS.items():
            with SessionLocal() as s:
                s.execute(delete(CacheEntry))
                s.commit()
            statements.clear()
            full = client.get(seeded[mid][1])
            n_full = len(statements)
            statements.clear()
            etag = full.headers["ETag"]
            revalidated = client.get(seeded[mid][1], headers={"If-None-Match": etag})
            assert (full.status_code, revalidated.status_code) == (200, 304), mid
            counted[mid] = (
                n_full if full_budget is not None else None,
                len(statements),
            )
    finally:
        event.remove(engine, "before_cursor_execute", grab)

    for mid, (full_budget, revalidation_budget) in MESSAGE_PAGE_STATEMENTS.items():
        full_count, revalidation_count = counted[mid]
        if full_budget is not None:
            assert 0 < full_count <= full_budget, counted
        assert 0 < revalidation_count <= revalidation_budget, counted
