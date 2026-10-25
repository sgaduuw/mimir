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
    # In code spans, or a Markdown renderer drops `<inbox>` as an HTML tag.
    assert len(re.findall(r"`http://localhost/<inbox>/[^`\s]+`", body)) == 6


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
    assert checked == 4  # message and thread, each in HTML and Markdown


def test_llms_txt_urls_use_the_configured_site_base_verbatim(client, monkeypatch):
    """Every URL is absolute on SITE_BASE_URL, and text/plain is not
    HTML-escaped: an `&` in the base or the site name stays an `&`."""
    monkeypatch.setattr(settings, "site_base_url", "https://lists.example.org/a&b/")
    monkeypatch.setattr(settings, "site_name", "Lists & <Co>")
    body = client.get("/llms.txt").get_data(as_text=True)
    assert body.startswith("# Lists & <Co>\n"), body[:40]
    urls = re.findall(r"https?://[^\s)`]+", body)
    assert len(urls) == 8, urls  # three shapes in two forms, sitemap, privacy
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
