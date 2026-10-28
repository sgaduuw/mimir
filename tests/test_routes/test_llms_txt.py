"""`/llms.txt` (#884): what the site is, where its Markdown pages are, and
the same content signals as `robots.txt`, so the two files never
disagree about what an agent may do with the content."""

import re
from datetime import date

import pytest

from mimir.config import settings
from tests.test_routes._helpers import _ingest_one_article


def _star_signal(robots_body: str) -> str | None:
    """The `*` stanza's Content-Signal value, or None when it has none."""
    stanza = robots_body.split("User-agent: *\n", 1)[1].split("\n\n", 1)[0]
    m = re.search(r"^Content-Signal: (.+)$", stanza, re.MULTILINE)
    return m.group(1) if m else None


def _llms_signal(body: str) -> str | None:
    found = re.findall(r"^Content-Signal: (.+)$", body, re.MULTILINE)
    assert len(found) <= 1, found
    return found[0] if found else None


def test_llms_txt_is_served_as_plain_text(client):
    r = client.get("/llms.txt")
    # 200 itself, not the `/<inbox_name>` route's redirect or 404 (#827).
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert r.headers["Cache-Control"] == "public, max-age=300"
    body = r.get_data(as_text=True)
    assert body.startswith("# ")
    assert body.endswith("\n")  # like robots.txt; Jinja drops the last one
    assert "http://localhost/sitemap.xml" in body
    assert "http://localhost/messages-sitemap.xml" in body
    # In code spans, or a Markdown renderer drops `<inbox>` as an HTML tag.
    assert (
        len(re.findall(r"`http://localhost/<inbox>/[^`\s]+`", body)) == 7
    )  # six message shapes, the subsystem index


@pytest.mark.parametrize("change", ["seeded", "edited", "cleared"])
def test_llms_txt_restates_the_robots_txt_content_signal(client, change):
    from mimir import robots

    if change == "edited":
        robots.update_rule("*", set_content_signal={"ai-input": "yes"})
    elif change == "cleared":
        robots.update_rule("*", clear_all_content_signals=True)
    robots_signal = _star_signal(client.get("/robots.txt").get_data(as_text=True))
    # Precondition: each case is the state it names.
    if change == "cleared":
        assert robots_signal is None
    else:
        assert robots_signal is not None
        assert ("ai-input=yes" in robots_signal.split(", ")) == (change == "edited")
    r = client.get("/llms.txt")
    assert r.status_code == 200  # a 404 has no signal either
    assert _llms_signal(r.get_data(as_text=True)) == robots_signal


def test_llms_txt_url_shapes_resolve(client, tmp_path):
    """Every advertised shape, filled in for a real message, is a page
    that answers: the shapes and the routes cannot drift apart."""
    _, url = _ingest_one_article(tmp_path, "alpha", "llms-shape@example.com")
    _, inbox, year, month, art_id = url.split("/")
    body = client.get("/llms.txt").get_data(as_text=True)
    shapes = re.findall(r"http://localhost(/<inbox>/[^\s)`]+)", body)
    md = [s for s in shapes if s.endswith(".md")]
    assert set(md) == {
        "/<inbox>/<YYYY>/<MM>/<id>.md",
        "/<inbox>/<YYYY>/<MM>/<id>/t.md",
        "/<inbox>/<YYYY>/<MM>/<id>/t/<page>.md",
    }
    checked = 0
    for shape in shapes:
        if "<page>" in shape:
            continue  # a single message has no second page
        path = (
            shape.replace("<inbox>", inbox)
            .replace("<YYYY>", year)
            .replace("<MM>", month)
            .replace("<id>", art_id)
        )
        assert client.get(path).status_code == 200, path
        checked += 1
    assert checked == 5  # message and thread in two forms, the subsystem index


def test_llms_txt_urls_use_the_configured_site_base_verbatim(client, monkeypatch):
    """Every URL is absolute on SITE_BASE_URL, and text/plain is not
    HTML-escaped: an `&` in the base or the site name stays an `&`."""
    monkeypatch.setattr(settings, "site_base_url", "https://lists.example.org/a&b/")
    monkeypatch.setattr(settings, "site_name", "Lists & <Co>")
    body = client.get("/llms.txt").get_data(as_text=True)
    assert body.startswith("# Lists & <Co>\n"), body[:40]
    urls = re.findall(r"https?://[^\s)`]+", body)
    # Three shapes in two forms, the subsystem and maintainer shapes, two
    # inboxes, home, merge window, two sitemaps and privacy.
    assert len(urls) == 6 + 2 + 2 + 1 + 1 + 2 + 1, urls
    assert all(u.startswith("https://lists.example.org/a&b/") for u in urls), urls
    assert "localhost" not in body and "&amp;" not in body


def test_llms_txt_later_thread_page_shape_is_thread_page_url(client, tmp_path):
    """The one shape the resolve test skips: filled in, it is exactly
    what `thread_page_url` builds for page 2, in both forms."""
    from mimir.web.urls import markdown_path, thread_page_url

    _, url = _ingest_one_article(tmp_path, "alpha", "llms-page@example.com")
    _, inbox, year, month, art_id = url.split("/")
    body = client.get("/llms.txt").get_data(as_text=True)
    paged = re.findall(r"http://localhost(/<inbox>/[^\s)`]*<page>[^\s)`]*)", body)
    filled = {
        s.replace("<inbox>", inbox)
        .replace("<YYYY>", year)
        .replace("<MM>", month)
        .replace("<id>", art_id)
        .replace("<page>", "2")
        for s in paged
    }
    want = thread_page_url(int(art_id), date(int(year), int(month), 1), inbox, page=2)
    assert filled == {want, markdown_path(want)}, (filled, want)


def _section(body: str, heading: str) -> list[str]:
    """The lines of one `## heading` section, blank lines dropped."""
    found = re.findall(
        rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL
    )
    assert len(found) == 1, (heading, found)
    return [line for line in found[0].splitlines() if line]


def _links(lines: list[str]) -> list[tuple[str, str, str]]:
    """`- [text](url): description` lines as (text, url, description)."""
    out = []
    for line in lines:
        m = re.fullmatch(r"- \[([^\]]+)\]\(([^)]+)\)(?:: (.*))?", line)
        assert m, line
        out.append((m[1], m[2], m[3] or ""))
    return out


def _add_inbox(name, *, last_article_date=None, list_address=None):
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox

    with SessionLocal() as s:
        s.add(
            Inbox(
                name=name,
                mirror_path=f"/tmp/{name}",
                upstream_url=f"https://lore.example.org/{name}/",
                list_address=list_address,
                last_article_date=last_article_date,
            )
        )
        s.commit()


def test_llms_txt_lists_exactly_the_inboxes_with_messages(client):
    from datetime import UTC, datetime

    _add_inbox("aardvark", last_article_date=datetime(2024, 1, 1, tzinfo=UTC))
    _add_inbox("empty-one")  # no messages: the dashboard is a blank page
    body = client.get("/llms.txt").get_data(as_text=True)
    names = [name for name, _, _ in _links(_section(body, "Lists"))]
    # Sorted by name, and the empty inbox is left out.
    assert names == ["aardvark", "alpha", "beta"], names


def test_llms_txt_list_line_names_address_and_upstream(client):
    from datetime import UTC, datetime

    when = datetime(2024, 1, 1, tzinfo=UTC)
    _add_inbox("with-addr", last_article_date=when, list_address="w@lists.example.org")
    _add_inbox("no-addr", last_article_date=when)
    body = client.get("/llms.txt").get_data(as_text=True)
    by_name = {n: d for n, _, d in _links(_section(body, "Lists"))}
    assert by_name["with-addr"] == (
        "w@lists.example.org, mirrored from lore.example.org/with-addr"
    )
    # No address: say only what is known.
    assert by_name["no-addr"] == "mirrored from lore.example.org/no-addr"


def test_llms_txt_list_links_equal_the_dashboard_canonical(client):
    """Byte-identical, not equivalent: the link and the page's own
    `<link rel="canonical">` are one spelling (AGENTS.md emitter/acceptor)."""
    body = client.get("/llms.txt").get_data(as_text=True)
    lists = _links(_section(body, "Lists"))
    assert len(lists) == 2  # both seeded inboxes, or the loop proves nothing
    for name, url, _ in lists:
        page = client.get(f"/{name}/")
        assert page.status_code == 200
        found = re.findall(
            r'<link rel="canonical" href="([^"]+)"', page.get_data(as_text=True)
        )
        assert found == [url], (found, url)
        # Both sides come from `inbox_path`, so they would agree on any
        # spelling; the literal pins the spelling itself.
        assert url == f"http://localhost/{name}/"


def test_llms_txt_page_links_answer_200_and_shapes_have_routes(client):
    body = client.get("/llms.txt").get_data(as_text=True)
    lines = _section(body, "Pages mimir adds over lore")
    links = _links([line for line in lines if line.startswith("- [")])
    assert [t for t, _, _ in links] == ["Home", "Merge window"]
    for _, url, _ in links:
        assert client.get(url.removeprefix("http://localhost")).status_code == 200, url
    shapes = [
        re.search(r"`http://localhost(/[^`]+)`", line)[1]
        for line in lines
        if not line.startswith("- [")
    ]
    assert shapes == ["/<inbox>/subsystem/", "/maintainers/<address>"]
    adapter = client.application.url_map.bind("localhost")
    for shape in shapes:
        path = shape.replace("<inbox>", "alpha").replace("<address>", "a@b.example")
        # The route exists; the page behind a made-up address may still 404.
        adapter.match(path)


def test_llms_txt_optional_section_holds_sitemaps_and_privacy(client):
    body = client.get("/llms.txt").get_data(as_text=True)
    optional = _links(_section(body, "Optional"))
    assert [t for t, _, _ in optional] == ["Sitemap", "Message sitemap", "Privacy"]
    assert body.rstrip().splitlines()[-1].startswith("- [Privacy]")  # last section


def test_llms_txt_runs_one_inbox_query_however_many_inboxes(client):
    """No N+1: the count of statements reading `inboxes` does not grow."""
    from datetime import UTC, datetime

    from sqlalchemy import event

    from mimir.extensions import SessionLocal

    def inbox_selects() -> int:
        seen: list[str] = []

        def grab(conn, cursor, statement, parameters, context, executemany):
            if re.search(r"\bFROM inboxes\b", statement):
                seen.append(statement)

        engine = SessionLocal().get_bind()
        event.listen(engine, "before_cursor_execute", grab)
        try:
            assert client.get("/llms.txt").status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", grab)
        return len(seen)

    before = inbox_selects()
    for i in range(5):
        _add_inbox(f"extra-{i}", last_article_date=datetime(2024, 1, 1, tzinfo=UTC))
    assert (before, inbox_selects()) == (1, 1)
