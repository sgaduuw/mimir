"""#852: an address used as a display name is no display name.

A non-allowlisted address never appears on a page, in JSON-LD or in a
feed, whether it sits in the angle brackets or in the quoted name.
"""

import xml.etree.ElementTree as ET

import pytest

from mimir.config import settings
from tests.test_routes._helpers import _ingest_one_article, _json_ld_blocks

NS = {"a": "http://www.w3.org/2005/Atom"}
SELF = '"s@private.example" <s@private.example>'
LISTED = '"a@other.example via list" <list@lists.example>'


def _surfaces(client, tmp_path, author):
    """Page HTML, thread JSON-LD graph and Atom feed text for one sender."""
    _, url = _ingest_one_article(tmp_path, "alpha", "addr852@x", author=author)
    html = client.get(url).data.decode()
    graph = _json_ld_blocks(html)[0]["@graph"]
    feed = client.get("/alpha/feed.atom").get_data(as_text=True)
    return html, graph, feed


@pytest.mark.parametrize(
    ("author", "addrs"),
    [
        (SELF, ["s@private.example"]),
        (LISTED, ["a@other.example", "list@lists.example"]),
    ],
)
def test_non_allowlisted_address_in_name_leaks_nowhere(
    client, tmp_path, monkeypatch, author, addrs
):
    monkeypatch.setattr(settings, "email_allowlist", [])
    html, graph, feed = _surfaces(client, tmp_path, author)
    leaked = {
        surface: [a for a in addrs if a in text]
        for surface, text in (("page", html), ("json-ld", str(graph)), ("atom", feed))
    }
    assert leaked == {"page": [], "json-ld": [], "atom": []}
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert posting["author"] == {"@type": "Person"}
    entry = ET.fromstring(feed).find("a:entry", NS)
    assert entry.find("a:author", NS) is None


def test_allowlisted_address_as_name_stays_named_by_address(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "email_allowlist", ["@open.example"])
    html, graph, feed = _surfaces(
        client, tmp_path, '"ok@open.example" <ok@open.example>'
    )
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert posting["author"]["name"] == "ok@open.example"
    entry = ET.fromstring(feed).find("a:entry", NS)
    assert entry.findtext("a:author/a:name", namespaces=NS) == "ok@open.example"
    assert "ok@open.example" in html


@pytest.mark.parametrize("name", ["John @ Company", "Jane Doe"])
def test_name_that_is_not_an_address_is_unchanged(client, tmp_path, monkeypatch, name):
    monkeypatch.setattr(settings, "email_allowlist", [])
    html, graph, feed = _surfaces(client, tmp_path, f'"{name}" <j@private.example>')
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert posting["author"]["name"] == name
    assert f"{name} &lt;hidden&gt;" in html
    entry = ET.fromstring(feed).find("a:entry", NS)
    assert entry.findtext("a:author/a:name", namespaces=NS) == name


@pytest.mark.parametrize(
    "author",
    [
        # Unparseable by the strict parseaddr: no address at all.
        "s@private.example <s@private.example>",
        "=?utf-8?q?s=40private.example?= <s@private.example>",
        "=?utf-8?q?Doe=2C_J=C3=BCrgen?= <s@private.example>",
        "Jane s@private.example <s@private.example>",
        # Parseable, with an address in a trailing comment.
        '"Jane" <s@private.example> (s@private.example)',
    ],
)
def test_odd_from_headers_leak_nowhere(client, tmp_path, monkeypatch, author):
    monkeypatch.setattr(settings, "email_allowlist", [])
    html, graph, feed = _surfaces(client, tmp_path, author)
    leaked = [
        surface
        for surface, text in (("page", html), ("json-ld", str(graph)), ("atom", feed))
        if "s@private.example" in text
    ]
    assert leaked == []


def test_allowlisted_sender_comment_does_not_print_foreign_address(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "email_allowlist", ["@open.example"])
    html, _, _ = _surfaces(
        client, tmp_path, '"Jane" <ok@open.example> (s@private.example)'
    )
    assert "ok@open.example" in html
    assert "s@private.example" not in html


def test_display_name_check_is_linear():
    import time

    from mimir.web.filters import _EMAIL_TOKEN_RE

    t = time.perf_counter()
    _EMAIL_TOKEN_RE.findall("a" * 40_000)
    assert time.perf_counter() - t < 0.2


def test_allowlisted_sender_with_foreign_address_in_name_shows_only_its_own(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "email_allowlist", ["@open.example"])
    html, _, _ = _surfaces(
        client, tmp_path, '"s@private.example via list" <ok@open.example>'
    )
    assert "ok@open.example" in html
    assert "s@private.example" not in html
