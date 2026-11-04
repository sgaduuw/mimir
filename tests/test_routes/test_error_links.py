"""The 404 page's way forward (#1002, #984).

It offers the front page, the Message-ID form, and links built from the
URL, but only to pages that exist: the same predicates the routes use
decide both, so the page never links to another 404. It stays cheap (a
crawler probing `/wp-login.php` costs no query) and cannot fail on its own
lookup.
"""

import pytest
from sqlalchemy import event

from tests._dom import parse


def _get(client, path, **kw):
    response = client.get(path, **kw)
    assert response.status_code == 404, path
    return response.get_data(as_text=True)


def _links(html):
    page = parse(html)
    main = page.find("section", cls="error-page")
    return {a.text(): a.attrs["href"] for a in main.find_all("a")}


def _statements(client, path):
    from mimir.extensions import engine

    seen = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", grab)
    try:
        response = client.get(path)
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    return response, seen


def test_every_404_offers_the_front_page_and_the_lookup_form(client):
    html = _get(client, "/no/such/route/exists/here")
    page = parse(html)
    main = page.find("section", cls="error-page")
    assert main.find("h1").text() == "Page not found"
    assert main.find("p", cls="error-code").text() == "404"
    assert _links(html)["All inboxes"] == "/"
    forms = main.find_all("form", cls="message-id-form")
    assert len(forms) == 1
    assert forms[0].attrs["action"] == "/m/" and forms[0].attrs["method"] == "get"


def test_a_message_shaped_path_says_the_message_is_not_here(client):
    page = parse(_get(client, "/alpha/2024/3/9999"))
    assert page.find("section", cls="error-page").find("h1").text() == (
        "This message isn't in the archive"
    )


def test_links_appear_for_a_valid_inbox_and_month(client):
    links = _links(_get(client, "/alpha/2024/3/9999"))
    assert links == {
        "March 2024 in alpha": "/alpha/2024/03/",
        "The alpha dashboard": "/alpha/",
        "All inboxes": "/",
    }


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # An inbox that does not exist: nothing built from the URL.
        ("/nosuch/2024/03/1", {"All inboxes": "/"}),
        # The inbox exists, the month is out of range: dashboard only.
        ("/alpha/1900/03/1", {"The alpha dashboard": "/alpha/", "All inboxes": "/"}),
        ("/alpha/2024/13/1", {"The alpha dashboard": "/alpha/", "All inboxes": "/"}),
        ("/alpha/2024/00/1", {"The alpha dashboard": "/alpha/", "All inboxes": "/"}),
    ],
)
def test_links_only_to_pages_that_exist(client, path, expected):
    assert _links(_get(client, path)) == expected


def test_every_link_the_404_emits_is_a_page_that_serves(client):
    """Emitter equals acceptor, as a property: follow each link."""
    followed = 0
    for path in (
        "/alpha/2024/3/9999",
        "/alpha/2024/03/9999/t",
        "/beta/2023/12/1",
        "/alpha/1996/1/2",
        "/alpha/1900/03/1",
        "/alpha/2024/13/1",
        "/nosuch/2024/03/1",
        "/alpha/no/such/page",
    ):
        for href in _links(_get(client, path)).values():
            assert client.get(href).status_code == 200, (path, href)
            followed += 1
    assert followed >= 15


def test_a_probe_costs_no_query_and_an_inbox_shaped_name_costs_one(client):
    """Paths that no route matches, so any inbox query is the 404 page's own
    (a route that matches `/<inbox>/...` looks the inbox up itself first)."""
    # The positive control first: the capture does see the lookup.
    _, shaped = _statements(client, "/nosuch/x/y/z/w")
    assert len([s for s in shaped if "FROM inboxes" in s]) == 1
    for path in ("/.git/a/b/c/d", "/.env/x/y", "/a_b/c/d/e", "/Alpha/x/y/z/w"):
        _, seen = _statements(client, path)
        assert seen == [], path


def test_a_failing_lookup_still_renders_the_404(client, monkeypatch):
    import mimir.web.error_links as error_links

    class Boom:
        def __enter__(self):
            raise RuntimeError("database is down")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(error_links, "SessionLocal", lambda: Boom())
    response = client.get("/alpha/2024/3/9999")
    assert response.status_code == 404
    page = parse(response.get_data(as_text=True))
    # Nothing built from the URL, but the page and the form are there.
    assert _links(response.get_data(as_text=True)) == {"All inboxes": "/"}
    assert page.find("form", cls="message-id-form")


def test_the_path_is_escaped_and_cut_short(client):
    html = _get(client, "/<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    long = _get(client, "/x/" + "a" * 500)
    shown = parse(long).find("code", cls="error-path").text()
    assert shown.endswith("…") and len(shown) <= 121
    assert 'style="' not in long


def test_the_form_is_prefilled_from_an_at_segment_and_from_a_missed_lookup(client):
    def prefill(html):
        return parse(html).find("input", id="message-id-input").attrs.get("value")

    assert (
        prefill(_get(client, "/some/list/a-b.1@lore.example/")) == "a-b.1@lore.example"
    )
    assert (
        prefill(_get(client, "/m/", query_string={"id": "gone@x.org"})) == "gone@x.org"
    )
    assert prefill(_get(client, "/no/at/here")) in (None, "")
    long = _get(client, "/m/", query_string={"id": "q" * 2000})
    assert len(prefill(long)) == 998
    hostile = _get(client, "/m/", query_string={"id": '"><b>x</b>'})
    assert "<b>x</b>" not in hostile


def test_no_link_on_a_404_leads_into_the_lookup_space(client):
    """Crawl trap guard: the form is a form, never a link, so there is no
    chain of `?id=` URLs for a crawler to walk."""
    for path in ("/m/?id=a@b.c", "/x/y@z.org/", "/alpha/2024/3/9999"):
        hrefs = [a.attrs["href"] for a in parse(_get(client, path)).find_all("a")]
        assert not [h for h in hrefs if "id=" in h or h.startswith("/m/")], path


def test_410_page_has_no_form_and_no_lookup(client):
    from flask import abort

    app = client.application

    @app.route("/_gone")
    def _gone():
        abort(410)

    response, seen = _statements(client, "/_gone")
    assert response.status_code == 410
    assert (
        parse(response.get_data(as_text=True)).find_all("form", cls="message-id-form")
        == []
    )
    assert [s for s in seen if "FROM inboxes" in s] == []


def test_month_path_and_the_range_predicate_are_shared():
    from mimir.web.urls import _month_in_range, _month_path

    assert _month_path("lkml", 2026, 9) == "/lkml/2026/09/"
    assert _month_path("lkml", 1996, 12) == "/lkml/1996/12/"
    assert _month_in_range(2024, 1) and _month_in_range(1995, 12)
    assert not _month_in_range(1994, 6)
    assert not _month_in_range(2024, 0) and not _month_in_range(2024, 13)
    assert not _month_in_range(3000, 5)


def test_the_month_route_and_the_404_agree_on_the_range(client):
    """The same predicate decides the link and the page."""
    for year, month in ((1994, 6), (2024, 13), (2024, 0), (3000, 5)):
        assert client.get(f"/alpha/{year}/{month:02d}/").status_code == 404
    for year, month in ((1995, 1), (2024, 3)):
        assert client.get(f"/alpha/{year}/{month:02d}/").status_code == 200


def test_500_renders_plain_with_no_lookup(client):
    app = client.application
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.route("/_boom")
    def _boom():
        raise RuntimeError("boom")

    r, seen = _statements(client, "/_boom")
    html = r.get_data(as_text=True)
    assert r.status_code == 500
    assert "message-id-form" not in html and "error-code" not in html
    assert "Back to" in html
    assert [s for s in seen if "FROM inboxes" in s] == []
    assert "Cache-Control" not in r.headers
