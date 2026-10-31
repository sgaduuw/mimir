"""The thread page's message cards (#989): author first, "replied to" from
the parent, the subject heading only where it differs."""

import json
import re
from datetime import datetime

from mimir.config import settings
from mimir.web.urls import thread_page_url

ROOT_SUBJECT = "Private Memory Nodes"


def _seed_chat(tmp_path, edges, *, authors=None, subjects=None):
    """`edges` is `(message_id, parent_message_id|None)` in arrival order.
    `authors` and `subjects` override a message's From and Subject.
    Returns `{message_id: (article_id, url)}`."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", edges)
    authors = authors or {}
    subjects = {edges[0][0]: ROOT_SUBJECT, **(subjects or {})}
    with SessionLocal() as s:
        for mid, (aid, _url) in seeded.items():
            a = s.get(Article, aid)
            a.author = authors.get(mid, "Pat Poster <pat@example.org>")
            a.subject = subjects.get(mid, f"Re: {ROOT_SUBJECT}")
        s.commit()
    return seeded


def _card(html, article_id):
    m = re.search(
        rf'<article class="thread-message" id="m{article_id}".*?</article>',
        html,
        re.DOTALL,
    )
    assert m, f"no card for {article_id}"
    return m.group(0)


def _replied_to(card):
    m = re.search(
        r'<span class="replied-to">replied to <a href="([^"]+)">([^<]+)</a>', card
    )
    return (m.group(1), m.group(2)) if m else None


CHAIN = [("r@x", None), ("a@x", "r@x"), ("b@x", "a@x"), ("c@x", "b@x")]
WHO = {
    "r@x": "Ada Lovelace <ada@example.org>",
    "a@x": "Bob Barker <bob@example.org>",
    "b@x": "Cy Young <cy@example.org>",
    "c@x": "Di Prince <di@example.org>",
}


def test_a_reply_names_who_it_answers_and_links_the_card(client, tmp_path):
    seeded = _seed_chat(tmp_path, CHAIN, authors=WHO)
    html = client.get(seeded["r@x"][1] + "/t").get_data(as_text=True)
    assert _replied_to(_card(html, seeded["r@x"][0])) is None
    for mid, parent in [("a@x", "r@x"), ("b@x", "a@x"), ("c@x", "b@x")]:
        href, name = _replied_to(_card(html, seeded[mid][0]))
        assert href == f"#m{seeded[parent][0]}", mid
        assert name == WHO[parent].split(" <")[0], mid


def test_a_card_leads_with_the_author_a_decorative_avatar_and_two_links(
    client, tmp_path
):
    seeded = _seed_chat(tmp_path, CHAIN, authors=WHO)
    aid, url = seeded["a@x"]
    card = _card(client.get(seeded["r@x"][1] + "/t").get_data(as_text=True), aid)
    head = re.search(r"<header>(.*?)</header>", card, re.DOTALL).group(1)
    assert re.search(r'<span class="avatar" aria-hidden="true">B</span>', head)
    assert re.search(r"<strong>Bob Barker</strong>", head)
    # The date links the message's own page (the crawl path); `#` is the
    # permalink to this card.
    assert re.search(rf'<a class="card-date" href="{re.escape(url)}"><time', head)
    assert f'<a class="permalink" href="#m{aid}"' in head
    assert head.index("avatar") < head.index("Bob Barker") < head.index("<time")


def test_a_parent_on_another_page_links_to_that_page(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = _seed_chat(tmp_path, CHAIN, authors=WHO)
    root_id, root_url = seeded["r@x"]
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        root = s.get(Article, root_id)
        page1 = thread_page_url(root.id, root.date, "alpha", 1)
        page2 = thread_page_url(root.id, root.date, "alpha", 2)
    html = client.get(page2).get_data(as_text=True)
    # b's parent a is on page 1; c's parent b is on this page.
    assert _replied_to(_card(html, seeded["b@x"][0])) == (
        f"{page1}#m{seeded['a@x'][0]}",
        "Bob Barker",
    )
    assert _replied_to(_card(html, seeded["c@x"][0])) == (
        f"#m{seeded['b@x'][0]}",
        "Cy Young",
    )
    # The HTMX slice carries the same markers for its own articles.
    sliced = client.get(page2, headers={"HX-Request": "true"}).get_data(as_text=True)
    assert _replied_to(_card(sliced, seeded["b@x"][0])) == _replied_to(
        _card(html, seeded["b@x"][0])
    )


def test_no_marker_for_an_unknown_parent_or_a_self_parent(client, tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    # A root whose parent is not in the archive, and a message that
    # names itself.
    off = _seed_chat(tmp_path / "one", [("r@x", "ghost@nowhere"), ("s@x", "r@x")])
    html = client.get(off["r@x"][1] + "/t").get_data(as_text=True)
    assert _replied_to(_card(html, off["r@x"][0])) is None
    assert _replied_to(_card(html, off["s@x"][0])) is not None  # precondition
    own = _seed_chat(tmp_path / "two", [("me@x", "me@x")])
    html = client.get(own["me@x"][1] + "/t").get_data(as_text=True)
    assert _replied_to(_card(html, own["me@x"][0])) is None


def test_a_thread_parent_cycle_renders_and_each_end_names_the_other(client, tmp_path):
    seeded = _seed_chat(
        tmp_path,
        [("y1@x", "y3@x"), ("y2@x", "y1@x"), ("y3@x", "y2@x")],
        authors={
            "y1@x": "One <1@e.org>",
            "y2@x": "Two <2@e.org>",
            "y3@x": "Three <3@e.org>",
        },
    )
    resp = client.get(seeded["y1@x"][1] + "/t", follow_redirects=True)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    names = {
        mid: _replied_to(_card(html, aid))[1]
        for mid, (aid, _) in seeded.items()
        if f'id="m{aid}"' in html
    }
    assert names == {"y1@x": "Three", "y2@x": "One", "y3@x": "Two"}


def test_the_subject_heading_shows_only_where_it_differs(client, tmp_path):
    seeded = _seed_chat(
        tmp_path,
        CHAIN,
        authors=WHO,
        subjects={
            "a@x": f"Re:  Re: {ROOT_SUBJECT.upper()}",
            "b@x": "[PATCH 1/2] a different thing",
        },
    )
    html = client.get(seeded["r@x"][1] + "/t").get_data(as_text=True)
    headings = {
        mid: re.search(r"<h2[^>]*>(.*?)</h2>", _card(html, seeded[mid][0]), re.DOTALL)
        for mid in seeded
    }
    assert headings["r@x"] is None  # the root: the h1 says it
    assert headings["a@x"] is None  # `Re:` stripped, case folded
    assert headings["c@x"] is None
    assert headings["b@x"] is not None
    assert seeded["b@x"][1] in headings["b@x"].group(1)  # still the message link
    assert "a different thing" in headings["b@x"].group(1)
    # One h1, and every message's own page is still linked from the page.
    assert len(re.findall(r"<h1", html)) == 1
    for aid, url in seeded.values():
        assert f'href="{url}"' in html, url


def test_the_title_comparison_is_linear_on_a_hostile_subject(client, tmp_path):
    import time

    seeded = _seed_chat(
        tmp_path,
        CHAIN,
        subjects={"a@x": "re: " * 300_000 + "x", "b@x": "Re:" + " " * 300_000 + "y"},
    )
    start = time.perf_counter()
    assert client.get(seeded["r@x"][1] + "/t").status_code == 200
    assert time.perf_counter() - start < 2


def test_a_rewritten_thread_parent_moves_the_page_etag_and_only_its_own(
    client, tmp_path, monkeypatch
):
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = _seed_chat(tmp_path, CHAIN, authors=WHO)
    with SessionLocal() as s:
        root = s.get(Article, seeded["r@x"][0])
        page1 = thread_page_url(root.id, root.date, "alpha", 1)
        page2 = thread_page_url(root.id, root.date, "alpha", 2)
    before = {p: client.get(p).headers["ETag"] for p in (page1, page2)}
    with SessionLocal() as s:  # c now answers the root, not b
        s.get(Article, seeded["c@x"][0]).thread_parent = "r@x"
        s.commit()
    after = {p: client.get(p).headers["ETag"] for p in (page1, page2)}
    assert after[page2] != before[page2]
    assert after[page1] == before[page1]  # a marker it does not render


def test_the_json_ld_breadcrumb_is_the_visible_one(client, tmp_path):
    seeded = _seed_chat(tmp_path, CHAIN, authors=WHO)
    html = client.get(seeded["r@x"][1] + "/t").get_data(as_text=True)
    nav = re.search(r'<nav aria-label="breadcrumb">(.*?)</nav>', html, re.DOTALL)
    visible = re.findall(r'<li[^>]*>(?:<a href="([^"]+)">)?([^<]+)', nav.group(1))
    ld = json.loads(
        re.search(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL
        ).group(1)
    )
    crumbs = next(g for g in ld["@graph"] if g["@type"] == "BreadcrumbList")
    items = [(i["name"], i["item"]) for i in crumbs["itemListElement"]]
    assert [n for n, _ in items] == [name for _, name in visible]
    base = items[0][1].rsplit("/alpha/", 1)[0]
    # Every visible link is the item's URL; the leaf is the page itself.
    for (href, _), (_, item) in zip(visible[:-1], items[:-1], strict=True):
        assert item == base + href
    canonical = re.search(r'<link rel="canonical" href="([^"]+)"', html).group(1)
    assert items[-1][1] == canonical


def test_a_message_landing_earlier_moves_an_off_page_parent_s_link_and_the_etag(
    client, tmp_path, monkeypatch
):
    """The page a parent sits on shifts only when a message joins the
    thread ahead of it, which moves `total_count` and so the validator."""
    from datetime import timedelta

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    # `n@x` starts as a thread of its own, then joins the root's, dated
    # before `a@x`.
    seeded = _seed_chat(tmp_path, [*CHAIN, ("n@x", None)], authors=WHO)
    root_id = seeded["r@x"][0]
    with SessionLocal() as s:
        root = s.get(Article, root_id)
        page1 = thread_page_url(root.id, root.date, "alpha", 1)
        page2 = thread_page_url(root.id, root.date, "alpha", 2)
    parent_a = f"#m{seeded['a@x'][0]}"
    before = client.get(page2)
    link_before = _replied_to(_card(before.get_data(as_text=True), seeded["b@x"][0]))
    assert link_before[0] == page1 + parent_a  # precondition: a is on page 1
    with SessionLocal() as s:
        n = s.get(Article, seeded["n@x"][0])
        n.thread_parent = "r@x"
        n.date = s.get(Article, seeded["a@x"][0]).date - timedelta(days=1)
        s.execute(
            select(ArticleList).where(ArticleList.article_id == n.id)
        ).scalar_one().thread_root_id = root_id
        s.commit()
    after = client.get(page2)
    link_after = _replied_to(_card(after.get_data(as_text=True), seeded["b@x"][0]))
    assert link_after[0] == parent_a  # a moved onto this page
    assert after.headers["ETag"] != before.headers["ETag"]


def test_off_page_parents_cost_one_statement_and_only_when_needed(
    client, tmp_path, monkeypatch
):
    from sqlalchemy import event

    import mimir.web.routes.thread as route
    from mimir.extensions import SessionLocal, engine
    from mimir.models import Article

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = _seed_chat(tmp_path, CHAIN, authors=WHO)
    with SessionLocal() as s:
        root = s.get(Article, seeded["r@x"][0])
        page1 = thread_page_url(root.id, root.date, "alpha", 1)
        page2 = thread_page_url(root.id, root.date, "alpha", 2)

    inside: list[int] = []
    statements: list[str] = []
    real = route.thread_parents

    def spy(*a, **k):
        before = len(statements)
        out = real(*a, **k)
        inside.append(len(statements) - before)
        return out

    monkeypatch.setattr(route, "thread_parents", spy)

    def grab(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", grab)
    try:
        r1 = client.get(page1)  # parents all on page 1
        assert inside == []
        r2 = client.get(page2)  # b's parent a is on page 1
        assert inside == [1]
        r304 = client.get(page2, headers={"If-None-Match": r2.headers["ETag"]})
        assert inside == [1]
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    assert (r1.status_code, r2.status_code, r304.status_code) == (200, 200, 304)


def test_on_the_recursive_walk_a_parent_on_another_page_links_to_it(
    client, tmp_path, monkeypatch
):
    """The fallback branch (`all_nodes`): one unrooted member sends the
    thread down `get_thread`, and the marker still names the page."""
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = _seed_chat(tmp_path, CHAIN, authors=WHO)
    with SessionLocal() as s:
        s.execute(
            select(ArticleList).where(ArticleList.article_id == seeded["c@x"][0])
        ).scalar_one().thread_root_id = None
        s.commit()
        root = s.get(Article, seeded["r@x"][0])
        page1 = thread_page_url(root.id, root.date, "alpha", 1)
        page2 = thread_page_url(root.id, root.date, "alpha", 2)
    html = client.get(page2).get_data(as_text=True)
    assert _replied_to(_card(html, seeded["b@x"][0])) == (
        f"{page1}#m{seeded['a@x'][0]}",
        "Bob Barker",
    )


def test_a_card_never_prints_a_hidden_address_and_names_a_nameless_sender_as_unknown(
    client, tmp_path
):
    """The card's byline and "replied to" use `display_name`: a sender with
    no usable name (bare address, email-shaped name) is "unknown sender",
    never their non-allowlisted address."""

    seeded = _seed_chat(
        tmp_path,
        CHAIN,
        authors={
            "r@x": "Ada Lovelace <ada@example.org>",
            "a@x": "bare@example.org",
            "b@x": '"named@example.org" <named@example.org>',
            "c@x": "Di Prince <di@example.org>",
        },
    )
    html = client.get(seeded["r@x"][1] + "/t").get_data(as_text=True)
    for mid in ("a@x", "b@x"):
        head = re.search(
            r"<header>(.*?)</header>", _card(html, seeded[mid][0]), re.DOTALL
        ).group(1)
        assert "<strong>unknown sender</strong>" in head, mid
        assert '<span class="avatar" aria-hidden="true">?</span>' in head, mid
        assert "@example.org" not in head, mid
    assert _replied_to(_card(html, seeded["b@x"][0]))[1] == "unknown sender"
    assert _replied_to(_card(html, seeded["c@x"][0]))[1] == "unknown sender"


def _date_text(html, article_id):
    """What a card's date shows: the text of its `<time>` (or bare `?`)."""
    link = re.search(
        r'<a class="card-date"[^>]*>(.*?)</a>', _card(html, article_id), re.DOTALL
    ).group(1)
    return re.sub(r"<[^>]+>", "", link).strip()


def test_a_card_date_shows_the_year_only_when_it_is_not_the_threads(client, tmp_path):
    """A thread that starts on 30 Dec 2025: a reply later that year has no
    year, one across the New Year does, a reply in a later year does, and a
    message with no date is a bare `?`."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    edges = [
        ("r@x", None),
        ("a@x", "r@x"),
        ("b@x", "a@x"),
        ("c@x", "b@x"),
        ("d@x", "c@x"),
    ]
    seeded = _seed_chat(tmp_path, edges)
    dates = {
        "r@x": datetime(2025, 12, 30, 10, 0),
        "a@x": datetime(2025, 12, 31, 23, 30),
        "b@x": datetime(2026, 1, 1, 0, 30),
        "c@x": datetime(2026, 3, 1, 9, 5),
        "d@x": None,
    }
    with SessionLocal() as s:
        for mid, when in dates.items():
            s.get(Article, seeded[mid][0]).date = when
        s.commit()
    url = f"/alpha/2025/12/{seeded['r@x'][0]}/t"  # the URL carries the date
    html = client.get(url).get_data(as_text=True)
    shown = {mid: _date_text(html, aid) for mid, (aid, _) in seeded.items()}
    assert shown == {
        "r@x": "Dec 30, 10:00",
        "a@x": "Dec 31, 23:30",
        "b@x": "Jan 1, 2026, 00:30",
        "c@x": "Mar 1, 2026, 09:05",
        "d@x": "?",
    }
    assert "<time>?" not in html  # a `<time>` needs a datetime
