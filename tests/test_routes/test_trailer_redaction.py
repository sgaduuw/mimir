"""#902: trailer lines hide a non-allowlisted address in two shapes the
first redaction missed: an address with an RFC 5322 atext character
outside the old class (`=`, `'`, ...), and an address used as the
trailer name. The HTML body, the `.md` pages and the lifecycle tooltip
share one rule.
"""

import html as htmllib
import json
import re
import time
from html.parser import HTMLParser

import pytest

from mimir.config import settings
from mimir.rendering.linkify import (
    _EMAIL_ANGLE_RE,
    EMAIL_LOCAL_PART,
    EMAIL_TOKEN_RE,
    plain_line,
)
from tests.test_routes._helpers import _ingest_one_article, _metadata_url

# Atext characters beyond the old `[A-Za-z0-9._%+-]`, one case per
# group so a regression names the character class that broke.
ODD_LOCAL_PARTS = [
    "x=y",
    "o'neil",
    "a!b",
    "a#b$c",
    "a&b*c",
    "a/b?c",
    "a^b`c",
    "a{b|c}d~e",
]
ROLES = ["Signed-off-by", "Reviewed-by"]


class _Attrs(HTMLParser):
    """Every `data-tooltip` value on the page, entities decoded."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.values.extend(v for k, v in attrs if k == "data-tooltip" and v)


def _tooltips(page: str) -> list[str]:
    p = _Attrs()
    p.feed(page)
    p.close()
    return p.values


# JSON-LD carries the body through its own redaction (#899), so it is
# outside this oracle, as in test_markdown_pages.
class _JsonLd(HTMLParser):
    """The text of each `<script type="application/ld+json">`."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.inside = False
        self.blocks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and ("type", "application/ld+json") in attrs:
            self.inside = True
            self.blocks.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.blocks[-1] += data


def _json_ld(page: str) -> str:
    """The page's JSON-LD, decoded, so an escaped character cannot hide
    a match. The HTML surfaces are the raw pages, JSON-LD included."""
    parser = _JsonLd()
    parser.feed(page)
    parser.close()
    blocks = [json.loads(b) for b in parser.blocks]
    assert blocks  # precondition: the page carries JSON-LD
    return json.dumps(blocks, ensure_ascii=False)


def _surfaces(client, tmp_path, body: bytes) -> dict[str, str]:
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "trailer902@example.com",
        subject="[PATCH] fix a thing",
        author="Author <author@other.example>",
        body=body,
    )
    page = client.get(url).get_data(as_text=True)
    metadata = client.get(_metadata_url(url))
    assert metadata.status_code == 200
    return {
        "metadata": metadata.get_data(as_text=True),
        "message": page,
        "message.json-ld": _json_ld(page),
        "thread": client.get(url + "/t").get_data(as_text=True),
        "message.md": client.get(url + ".md").get_data(as_text=True),
        "thread.md": client.get(url + "/t.md").get_data(as_text=True),
    }


def _leaks(surfaces: dict[str, str], address: str) -> list[str]:
    """Surfaces showing `address` as written or HTML-escaped."""
    forms = {address, htmllib.escape(address), htmllib.escape(address, quote=False)}
    return [name for name, text in surfaces.items() if any(f in text for f in forms)]


def _body(role: str, shape: str, address: str) -> bytes:
    line = {
        "angle": f"{role}: Joe User <{address}>",
        "name": f"{role}: {address} <{address}>",
    }[shape]
    return f"Fix it.\n\n{line}\nSigned-off-by: Author <author@kernel.org>\n".encode()


@pytest.mark.parametrize("local", ODD_LOCAL_PARTS)
@pytest.mark.parametrize("shape", ["angle", "name"])
@pytest.mark.parametrize("role", ROLES)
def test_non_allowlisted_trailer_address_shows_on_no_surface(
    client, tmp_path, monkeypatch, local, shape, role
):
    monkeypatch.setattr(settings, "email_allowlist", ["@kernel.org"])
    address = f"{local}@example.com"
    body = _body(role, shape, address)
    assert address.encode() in body  # precondition: the raw body has it
    surfaces = _surfaces(client, tmp_path, body)
    assert surfaces["message"].count("Author") >= 1  # the page rendered
    assert _leaks(surfaces, address) == []


@pytest.mark.parametrize("local", ["x=y", "o'neil", "joe"])
@pytest.mark.parametrize("shape", ["angle", "name"])
def test_allowlisted_trailer_address_still_shows(
    client, tmp_path, monkeypatch, local, shape
):
    monkeypatch.setattr(settings, "email_allowlist", ["@open.example"])
    address = f"{local}@open.example"
    body = _body("Signed-off-by", shape, address)
    surfaces = _surfaces(client, tmp_path, body)
    # The metadata fragment has no body; Signed-off-by alone adds no
    # reviewer tooltip. Keep the body's allowlisted-address contract.
    assert _leaks({k: v for k, v in surfaces.items() if k != "metadata"}, address) == [
        "message",
        "message.json-ld",
        "thread",
        "message.md",
        "thread.md",
    ]


def test_email_shaped_reviewer_name_is_dropped_from_the_tooltip(
    client, tmp_path, monkeypatch
):
    """The name is stored as written; only display hides it."""
    monkeypatch.setattr(settings, "email_allowlist", ["@open.example"])
    body = (
        b"Fix it.\n\n"
        b"Reviewed-by: gone@example.com <gone@example.com>\n"
        b"Reviewed-by: kept@open.example <kept@open.example>\n"
        b"Reviewed-by: Plain Name <plain@example.com>\n"
    )
    surfaces = _surfaces(client, tmp_path, body)
    for surface in ["thread", "metadata"]:
        tips = " ".join(_tooltips(surfaces[surface]))
        assert "Plain Name" in tips  # the tooltip rendered its names
        assert "kept@open.example" in tips
        assert "gone@example.com" not in tips


def test_bare_non_allowlisted_address_in_a_trailer_is_hidden(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "email_allowlist", ["@kernel.org"])
    body = b"Fix it.\n\nCc: lurker@example.com\nSigned-off-by: A <a@kernel.org>\n"
    assert b"lurker@example.com" in body
    surfaces = _surfaces(client, tmp_path, body)
    assert _leaks(surfaces, "lurker@example.com") == []
    assert "a@kernel.org" in surfaces["message.md"]


def _hide_all(address: str) -> str:
    return "<redacted>"


@pytest.mark.parametrize("local", ODD_LOCAL_PARTS)
def test_angle_regex_takes_the_whole_atext_local_part(local):
    m = _EMAIL_ANGLE_RE.search(f"Signed-off-by: J <{local}@example.com>")
    assert m is not None
    assert m.group(1) == f"{local}@example.com"


def test_plain_line_hides_name_and_angle_address():
    line = "Signed-off-by: s@example.com <s@example.com>"
    assert "s@example.com" not in plain_line(line, _hide_all)


# Check 7: each changed regex on its hostile shape, bounded loosely.
_REPEATS = 300_000


@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        (_EMAIL_ANGLE_RE, "<" + "a" * _REPEATS),
        (_EMAIL_ANGLE_RE, "<a" * _REPEATS),
        (_EMAIL_ANGLE_RE, "<" + "a." * _REPEATS + "@"),
        (EMAIL_TOKEN_RE, "a" * _REPEATS),
        (EMAIL_TOKEN_RE, "a@" * _REPEATS),
        (EMAIL_TOKEN_RE, "@a" * _REPEATS),
    ],
    # Short ids: the default spells each 300,000-character input into
    # the test name, and CI's verbose log spent eleven minutes on it.
    ids=[
        "angle-open-run",
        "angle-repeated-open",
        "angle-dotted-run",
        "token-run",
        "token-at-pairs",
        "token-leading-at",
    ],
)
def test_address_regexes_are_linear_on_hostile_input(pattern, text):
    t = time.perf_counter()
    pattern.findall(text)
    assert time.perf_counter() - t < 5


def test_local_part_constant_is_the_rfc_5322_atext_class():
    assert re.fullmatch(EMAIL_LOCAL_PART, "a!#$%&'*+/=?^_`{|}~-.z")
    assert not re.fullmatch(EMAIL_LOCAL_PART, "a b")
    assert not re.fullmatch(EMAIL_LOCAL_PART, "a<b")
    assert not re.fullmatch(EMAIL_LOCAL_PART, "a>b")
    assert not re.fullmatch(EMAIL_LOCAL_PART, 'a"b')
