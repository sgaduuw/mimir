"""#863: allowlist tokens match the address by structure, not substring."""

import pytest

from mimir.config import settings
from mimir.web.filters import _is_allowlisted
from tests.test_routes.test_address_display_name import _surfaces


@pytest.mark.parametrize(
    ("tokens", "address", "expected"),
    [
        (["@open.example"], "a@open.example", True),
        (["@open.example"], "A@Open.Example", True),
        (["@open.example"], "a@sub.open.example", False),
        (["@open.example"], "a@open.example.evil.example", False),
        (["@open.example"], "a@xopen.example", False),
        (["@open.example"], "open.example@evil.example", False),
        (["@open.example"], '"x@evil.example"@open.example', False),
        (["@open.example"], "x@evil.example@open.example", False),
        # A token without "@" is a domain, same as "@token".
        (["open.example"], "a@open.example", True),
        (["open.example"], "a@sub.open.example", False),
        (["open.example"], "a@xopen.example", False),
        (["open.example"], "open.example@evil.example", False),
        # Full address: exact only.
        (["a@open.example"], "a@open.example", True),
        (["a@open.example"], "A@OPEN.example", True),
        (["a@open.example"], "xa@open.example", False),
        (["a@open.example"], "a@open.example.evil.example", False),
        (["a@open.example"], "a@sub.open.example", False),
        # "local@": that local part at any domain.
        (["torvalds@"], "torvalds@linux-foundation.org", True),
        (["torvalds@"], "Torvalds@anywhere.example", True),
        (["torvalds@"], "xtorvalds@linux-foundation.org", False),
        (["torvalds@"], "torvalds2@linux-foundation.org", False),
    ],
)
def test_token_matches_by_structure(monkeypatch, tokens, address, expected):
    monkeypatch.setattr(settings, "email_allowlist", tokens)
    assert _is_allowlisted(address) is expected


def test_allowlisted_domain_is_visible(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "email_allowlist", ["@open.example"])
    html, _, _ = _surfaces(client, tmp_path, "Ann <a@open.example>")
    assert "a@open.example" in html


@pytest.mark.parametrize(
    ("author", "needle"),
    [
        ("Eve <a@open.example.evil.example>", "a@open.example.evil.example"),
        ("Bob <b@sub.open.example>", "b@sub.open.example"),
        ('Eve <"x@evil.example"@open.example>', "x@evil.example"),
    ],
    ids=["lookalike", "subdomain", "quoted-local-part"],
)
def test_non_matching_address_is_hidden_everywhere(
    client, tmp_path, monkeypatch, author, needle
):
    monkeypatch.setattr(settings, "email_allowlist", ["@open.example"])
    html, graph, feed = _surfaces(client, tmp_path, author)
    leaked = [
        surface
        for surface, text in (("page", html), ("json-ld", str(graph)), ("atom", feed))
        if needle in text
    ]
    assert leaked == []


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("greg@kernel.org", True),
        ("gregkh@suse.de", True),
        ("torvalds@osdl.org", True),
        ("torvalds@transmeta.com", True),
        ("torvalds@evil.example", False),
        ("gregkh@evil.example", False),
        ("a@vger.kernel.org", False),
    ],
)
def test_default_allowlist(address, expected):
    assert _is_allowlisted(address) is expected
