"""The thread page's outline (#989): a jump-to list of the thread's first
messages, with the patches as one row and the replies indented by depth."""

import re
from types import SimpleNamespace

import pytest

from mimir.config import settings
from mimir.web.routes import thread as thread_route
from mimir.web.urls import thread_page_url

EDGES = [
    ("c@x", None),
    ("p1@x", "c@x"),
    ("p2@x", "c@x"),
    ("q1@x", "c@x"),
    ("q2@x", "q1@x"),
    ("q3@x", "q2@x"),
    ("q4@x", "q1@x"),
]
WHO = {
    "c@x": "Ada Lovelace <ada@example.org>",
    "p1@x": "Ada Lovelace <ada@example.org>",
    "p2@x": "Ada Lovelace <ada@example.org>",
    "q1@x": "Bob Barker <bob@example.org>",
    "q2@x": "Cy Young <cy@example.org>",
    "q3@x": "Di Prince <di@example.org>",
    "q4@x": "Eve Adams <eve@example.org>",
}


def _seed_outline_thread(tmp_path, edges=EDGES):
    """A cover, two patches and replies q1 (depth 0), q2 (1), q3 (2) and
    q4 (1). Returns `seed_thread_shape`'s map."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", edges)
    series = {
        "c@x": ("[PATCH v2 0/2] foo: do the thing", 0),
        "p1@x": ("[PATCH v2 1/2] foo: first", 1),
        "p2@x": ("[PATCH v2 2/2] foo: second", 2),
    }
    with SessionLocal() as s:
        for mid, (aid, _url) in seeded.items():
            a = s.get(Article, aid)
            a.author = WHO.get(mid, "Pat Poster <pat@example.org>")
            if mid in series:
                a.subject, a.patch_series_position = series[mid]
                a.patch_series_key = "o" * 40
                a.patch_series_version = "v2"
            else:
                a.subject = "Re: [PATCH v2 0/2] foo: do the thing"
        s.commit()
    return seeded


def _outline(html):
    """`[(kind, depth, here, href, text)]` for the outline's rows, or None
    when the page has no outline."""
    m = re.search(r'<details class="thread-outline">(.*?)</details>', html, re.DOTALL)
    if m is None:
        return None
    return [
        (
            row.group(1),
            int(row.group(3)),
            "here" in row.group(2),
            row.group(4),
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", row.group(5))).strip(),
        )
        for row in re.finditer(
            r'<li class="outline-(\w+)([^"]*)" data-depth="(\d+)"><a href="([^"]+)">(.*?)</li>',
            m.group(1),
            re.DOTALL,
        )
    ]


def _get(client, url, **headers):
    return client.get(url, headers=headers)


def test_the_outline_lists_the_cover_the_patches_as_one_row_and_the_replies_by_depth(
    client, tmp_path
):
    seeded = _seed_outline_thread(tmp_path)
    html = _get(client, seeded["c@x"][1] + "/t").get_data(as_text=True)
    rows = _outline(html)
    assert rows is not None
    assert [(k, d) for k, d, *_ in rows] == [
        ("root", 0),
        ("patches", 0),
        ("reply", 0),  # q1 answers the cover, which is not a listed reply
        ("reply", 1),  # q2
        ("reply", 2),  # q3
        ("reply", 1),  # q4
    ]
    assert [t.split(" Jan")[0].split(" ·")[0] for *_, t in rows][2:] == [
        "Bob Barker",
        "Cy Young",
        "Di Prince",
        "Eve Adams",
    ]
    assert rows[0][4].startswith("Ada Lovelace")
    assert rows[1][4].startswith("Patches (2)")
    assert 'aria-label="Jump to' not in html  # the summary is the label
    assert re.search(r"<summary>Jump to a message</summary>", html)


def test_the_outline_stops_at_its_cap_and_says_how_many_are_not_listed(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(thread_route, "OUTLINE_ROWS", 4)
    monkeypatch.setattr(settings, "thread_view_render_cap", 3)
    seeded = _seed_outline_thread(tmp_path)
    root_id = seeded["c@x"][0]
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        root = s.get(Article, root_id)
        page2 = thread_page_url(root.id, root.date, "alpha", 2)
    html = _get(client, seeded["c@x"][1] + "/t").get_data(as_text=True)
    rows = _outline(html)
    # Listed: cover, p1, p2, q1. Three messages are not.
    assert [k for k, *_ in rows] == ["root", "patches", "reply", "more"]
    kind, _depth, _here, href, text = rows[-1]
    assert text.startswith("3 more messages")
    # The fifth message (q2) is the first of page 2.
    assert href == f"{page2}#m{seeded['q2@x'][0]}"
    # Page 2 reads its own copy of the same first rows: the same cap.
    later = _outline(_get(client, page2).get_data(as_text=True))
    assert [(k, d, t) for k, d, _h, _u, t in later] == [
        (k, d, t) for k, d, _h, _u, t in rows
    ]


def test_every_outline_link_names_the_page_that_holds_its_message(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = _seed_outline_thread(tmp_path)
    html = _get(client, seeded["c@x"][1] + "/t").get_data(as_text=True)
    rows = _outline(html)
    assert len(rows) >= 5  # precondition: links on and off this page
    off_page = 0
    for kind, _depth, _here, href, _text in rows:
        url, _, anchor = href.partition("#")
        target = html if not url else _get(client, url).get_data(as_text=True)
        off_page += bool(url)
        assert f'id="{anchor}"' in target, href
    assert off_page, "no link left the page"


def test_rows_on_this_page_are_marked(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "thread_view_render_cap", 3)
    seeded = _seed_outline_thread(tmp_path)
    rows = _outline(_get(client, seeded["c@x"][1] + "/t").get_data(as_text=True))
    # Page 1 holds the cover and both patches.
    assert [here for _k, _d, here, *_ in rows] == [
        True,
        True,
        False,
        False,
        False,
        False,
    ]


@pytest.mark.parametrize("n_replies", [0, 1])
def test_a_short_thread_has_no_outline(client, tmp_path, n_replies):
    edges = [("c@x", None), *[(f"q{i}@x", "c@x") for i in range(n_replies)]]
    seeded = _seed_outline_thread(tmp_path, edges)
    html = _get(client, seeded["c@x"][1] + "/t").get_data(as_text=True)
    assert "thread-outline" not in html


def test_the_threshold_is_five_messages(client, tmp_path):
    edges = [("c@x", None), *[(f"q{i}@x", "c@x") for i in range(3)]]  # four
    seeded = _seed_outline_thread(tmp_path, edges)
    url = seeded["c@x"][1] + "/t"
    assert _outline(_get(client, url).get_data(as_text=True)) is None
    assert thread_route.OUTLINE_MIN_MESSAGES == 5


def test_the_htmx_slice_and_the_markdown_page_have_no_outline(client, tmp_path):
    seeded = _seed_outline_thread(tmp_path)
    url = seeded["c@x"][1] + "/t"
    assert _outline(_get(client, url).get_data(as_text=True))  # precondition
    sliced = _get(client, url, **{"HX-Request": "true"}).get_data(as_text=True)
    assert "thread-outline" not in sliced
    assert "Jump to a message" not in client.get(url + ".md").get_data(as_text=True)


def test_the_outline_has_no_inline_style_or_script_and_starts_closed(client, tmp_path):
    seeded = _seed_outline_thread(tmp_path)
    html = _get(client, seeded["c@x"][1] + "/t").get_data(as_text=True)
    block = re.search(
        r'<details class="thread-outline"[^>]*>.*?</details>', html, re.DOTALL
    ).group(0)
    assert block.startswith('<details class="thread-outline">')
    assert "style=" not in block and "<script" not in block


def test_a_rewritten_parent_in_the_listed_rows_moves_every_page_s_etag(
    client, tmp_path, monkeypatch
):
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    monkeypatch.setattr(settings, "thread_view_render_cap", 3)
    seeded = _seed_outline_thread(tmp_path)
    with SessionLocal() as s:
        root = s.get(Article, seeded["c@x"][0])
        pages = [thread_page_url(root.id, root.date, "alpha", n) for n in (1, 2, 3)]
    before = [client.get(p).headers["ETag"] for p in pages]
    with SessionLocal() as s:  # q4 (page 3) now answers q2, not q1
        s.get(Article, seeded["q4@x"][0]).thread_parent = "q2@x"
        s.commit()
    after = [client.get(p).headers["ETag"] for p in pages]
    # Page 1 renders no card for q4: only the outline's depth saw it.
    assert after[0] != before[0]
    assert after[1] != before[1]


def test_a_rewritten_parent_past_the_cap_moves_no_other_page(
    client, tmp_path, monkeypatch
):
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    monkeypatch.setattr(thread_route, "OUTLINE_ROWS", 4)
    monkeypatch.setattr(settings, "thread_view_render_cap", 3)
    seeded = _seed_outline_thread(tmp_path)
    with SessionLocal() as s:
        root = s.get(Article, seeded["c@x"][0])
        page1 = thread_page_url(root.id, root.date, "alpha", 1)
    before = client.get(page1).headers["ETag"]
    with SessionLocal() as s:  # q4 is the 7th message: not listed
        s.get(Article, seeded["q4@x"][0]).thread_parent = "q2@x"
        s.commit()
    assert client.get(page1).headers["ETag"] == before


def _count_statements(client, url, **headers):
    from sqlalchemy import event

    from mimir.extensions import engine

    statements: list[str] = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    client.get(url, headers=headers)  # warm the caches a first render fills
    event.listen(engine, "before_cursor_execute", grab)
    try:
        full = client.get(url, headers=headers)
        n_full = len(statements)
        statements.clear()
        revalidated = client.get(
            url, headers={**headers, "If-None-Match": full.headers["ETag"]}
        )
        n_304 = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    assert (full.status_code, revalidated.status_code) == (200, 304)
    return n_full, n_304


def test_the_outline_costs_no_statement_on_page_one_and_one_on_later_pages(
    client, tmp_path, monkeypatch
):
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    monkeypatch.setattr(settings, "thread_view_render_cap", 3)
    seeded = _seed_outline_thread(tmp_path)
    with SessionLocal() as s:
        root = s.get(Article, seeded["c@x"][0])
        page1 = thread_page_url(root.id, root.date, "alpha", 1)
        page2 = thread_page_url(root.id, root.date, "alpha", 2)
    with_outline = {p: _count_statements(client, p) for p in (page1, page2)}
    assert _outline(client.get(page2).get_data(as_text=True))  # precondition
    monkeypatch.setattr(thread_route, "OUTLINE_MIN_MESSAGES", 10_000)
    without = {p: _count_statements(client, p) for p in (page1, page2)}
    assert _outline(client.get(page2).get_data(as_text=True)) is None
    assert with_outline[page1] == without[page1]
    assert with_outline[page2] == (without[page2][0] + 1, without[page2][1] + 1)


def test_the_htmx_slice_and_the_markdown_page_read_and_digest_nothing_for_the_outline(
    client, tmp_path, monkeypatch
):
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    monkeypatch.setattr(settings, "thread_view_render_cap", 3)
    seeded = _seed_outline_thread(tmp_path)
    with SessionLocal() as s:
        root = s.get(Article, seeded["c@x"][0])
        page1 = thread_page_url(root.id, root.date, "alpha", 1)
        page2 = thread_page_url(root.id, root.date, "alpha", 2)
    hx = {"HX-Request": "true"}
    probes = [(page2, hx), (page2 + ".md", {}), (page1, hx), (page1 + ".md", {})]
    with_ = [_count_statements(client, u, **h) for u, h in probes]
    etags = [client.get(u, headers=h).headers["ETag"] for u, h in probes]
    monkeypatch.setattr(thread_route, "OUTLINE_MIN_MESSAGES", 10_000)
    assert [_count_statements(client, u, **h) for u, h in probes] == with_
    # Nor does a parent the outline lists move them: q4 (page 3) is listed.
    monkeypatch.setattr(thread_route, "OUTLINE_MIN_MESSAGES", 5)
    with SessionLocal() as s:
        s.get(Article, seeded["q4@x"][0]).thread_parent = "q2@x"
        s.commit()
    assert [client.get(u, headers=h).headers["ETag"] for u, h in probes] == etags


def test_a_header_for_a_root_with_no_author_says_who_started_it(client, tmp_path):
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    seeded = _seed_outline_thread(tmp_path)
    with SessionLocal() as s:
        s.get(Article, seeded["c@x"][0]).author = None
        s.commit()
    html = _get(client, seeded["c@x"][1] + "/t").get_data(as_text=True)
    byline = re.search(r'<p class="thread-byline">(.*?)</p>', html, re.DOTALL).group(1)
    assert "Started by <strong>unknown sender</strong>" in byline


def test_outline_depth_counts_listed_ancestors_and_survives_cycles():
    from mimir.threading import outline_depths

    def n(i, parent):
        return SimpleNamespace(id=i, message_id=f"m{i}", thread_parent=parent)

    nodes = [
        n(1, None),
        n(2, "m1"),
        n(3, "m2"),
        n(4, "m9"),
        n(5, "m5"),
        n(6, "m7"),
        n(7, "m6"),
    ]
    depths = outline_depths(nodes)
    assert {i: depths[i] for i in (1, 2, 3, 4, 5)} == {
        1: 0,
        2: 1,
        3: 2,
        4: 0,  # its parent is not listed
        5: 0,  # its own parent
    }
    # A two-cycle: finite, and one end is under the other at most.
    assert {depths[6], depths[7]} <= {0, 1}


def test_outline_depth_is_linear_on_a_long_chain_and_a_long_cycle():
    import time

    from mimir.threading import outline_depths

    count = 50_000
    chain = [
        SimpleNamespace(
            id=i, message_id=f"m{i}", thread_parent=f"m{i - 1}" if i else None
        )
        for i in range(count)
    ]
    ring = [
        SimpleNamespace(id=i, message_id=f"m{i}", thread_parent=f"m{(i + 1) % count}")
        for i in range(count)
    ]
    start = time.perf_counter()
    assert outline_depths(chain)[count - 1] == count - 1
    outline_depths(ring)
    assert time.perf_counter() - start < 2


def test_a_thread_that_shrinks_between_the_reads_still_renders(
    client, tmp_path, monkeypatch
):
    """`total_count` is read before the validator and the whole thread's
    rows after it, with no snapshot between: a `reindex --from-scratch` can
    remove rows in between. The outline's "N more" row must come from the
    rows actually read, not from the count."""
    monkeypatch.setattr(thread_route, "OUTLINE_ROWS", 4)
    seeded = _seed_outline_thread(tmp_path)
    real = thread_route.thread_rows
    monkeypatch.setattr(thread_route, "thread_rows", lambda *a, **k: real(*a, **k)[:4])
    resp = client.get(seeded["c@x"][1] + "/t")
    assert resp.status_code == 200
    rows = _outline(resp.get_data(as_text=True))
    assert rows and rows[0][0] == "root"


def test_the_cte_fallback_lists_every_message_and_its_links_resolve(
    client, tmp_path, monkeypatch
):
    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = _seed_outline_thread(tmp_path)
    with SessionLocal() as s:
        s.execute(
            update(ArticleList)
            .where(ArticleList.article_id == seeded["q3@x"][0])
            .values(thread_root_id=None)
        )
        s.commit()
    html = _get(client, seeded["c@x"][1] + "/t").get_data(as_text=True)
    rows = _outline(html)
    # No kinds on the fallback: every message but the root is a reply row.
    assert [k for k, *_ in rows] == ["root"] + ["reply"] * 6
    for _k, _d, _h, href, _t in rows:
        url, _, anchor = href.partition("#")
        target = html if not url else _get(client, url).get_data(as_text=True)
        assert f'id="{anchor}"' in target, href


def test_a_thread_that_grows_between_the_reads_lists_no_empty_more_row(
    client, tmp_path, monkeypatch
):
    """A message ingested after the outline's rows are read and before
    `thread_rows` is not counted in `total_count`: no "0 more" row."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList

    seeded = _seed_outline_thread(tmp_path, EDGES[:5])  # five: all listed
    root_id = seeded["c@x"][0]
    real = thread_route.thread_rows

    def late_arrival(session, inbox, root_article_id):
        with SessionLocal() as s:
            root = s.get(Article, root_id)
            late = Article(
                message_id="late@x",
                subject="Re: late",
                author="Late <late@example.org>",
                date=root.date,
                thread_parent="c@x",
            )
            s.add(late)
            s.flush()
            s.add(
                ArticleList(
                    article_id=late.id,
                    inbox_id=inbox.id,
                    epoch="0.git",
                    commit_sha="f" * 40,
                    thread_root_id=root_id,
                )
            )
            s.commit()
        return real(session, inbox, root_article_id)

    monkeypatch.setattr(thread_route, "thread_rows", late_arrival)
    resp = client.get(seeded["c@x"][1] + "/t")
    assert resp.status_code == 200
    rows = _outline(resp.get_data(as_text=True))
    assert [r for r in rows if r[0] == "more"] == [], rows[-1]
