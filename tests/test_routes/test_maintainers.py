"""Tests for `/maintainers/<address>`: the global (cross-inbox)
maintainer profile page. See mimir/web/routes/maintainers.py and
mimir/templates/maintainer.html."""

import re

import pytest
from sqlalchemy import select

from mimir.models import Article, ArticleTrailer, Subsystem, SubsystemMaintainer
from tests.test_routes._helpers import _json_ld_blocks


def _add_subsystem(session, name, status, maintainers):
    """Insert a Subsystem + its M:/R: maintainer rows. `maintainers`
    is a list of (role, name, address) tuples; addresses are passed
    verbatim (not lowercased) to mirror how MAINTAINERS entries
    actually look. Mirrors tests/test_maintainer_directory.py's
    helper of the same name."""
    sub = Subsystem(name=name, status=status)
    for role, mname, addr in maintainers:
        sub.maintainers.append(SubsystemMaintainer(role=role, name=mname, address=addr))
    session.add(sub)
    session.commit()
    return sub


def _add_trailer(session, message_id, address, role="Reviewed-by", name=""):
    """Attach one ArticleTrailer to an already-seeded conftest article
    (art1..art4), keyed by the article's real message_id. Mirrors
    tests/test_maintainer_directory.py's helper of the same name."""
    article = session.execute(
        select(Article).where(Article.message_id == message_id)
    ).scalar_one()
    session.add(
        ArticleTrailer(
            article_id=article.id,
            role=role,
            name=name,
            address=address,
            address_normalized=address.lower(),
        )
    )
    session.commit()


def test_maintainer_view_200_shows_profile_and_reviewer_link(client, session):
    """A seeded M: maintainer with review-trailer activity on `alpha`
    renders 200 with their name, address, and a reviewer link into
    that inbox."""
    _add_subsystem(
        session,
        "BCACHEFS",
        "Supported",
        maintainers=[("M", "Kent Overstreet", "kent@kernel.org")],
    )
    # art1@example.com is seeded in the alpha inbox by conftest's
    # autouse _reset_db.
    _add_trailer(session, "art1@example.com", "kent@kernel.org")

    r = client.get("/maintainers/kent@kernel.org")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Kent Overstreet" in body
    assert "kent@kernel.org" in body
    assert "bcachefs" in body
    assert "/alpha/reviewer/kent@kernel.org" in body


def test_maintainer_view_no_review_activity_shows_muted_line(client, session):
    """A maintainer with a MAINTAINERS entry but no indexed review
    trailers still renders 200, with the empty-state line rather than
    an empty list."""
    _add_subsystem(
        session,
        "ZFS",
        "Maintained",
        maintainers=[("M", "Carol", "carol@kernel.org")],
    )

    r = client.get("/maintainers/carol@kernel.org")
    assert r.status_code == 200
    body = r.data.decode()
    assert "Carol" in body
    assert "No indexed review activity yet." in body


def test_maintainer_view_mixed_case_url_resolves(client, session):
    """A URL segment in a different case than the stored MAINTAINERS
    address still resolves: the route lowercases before matching
    `func.lower(SubsystemMaintainer.address)`."""
    _add_subsystem(
        session,
        "ADVANTECH",
        "Maintained",
        maintainers=[("M", "Foo Bar", "Foo.Bar@x.org")],
    )

    r = client.get("/maintainers/Foo.Bar@x.org")
    assert r.status_code == 200
    assert "Foo Bar" in r.data.decode()


def test_maintainer_view_404_unknown_address(client):
    """A well-formed address with no MAINTAINERS entry 404s."""
    assert client.get("/maintainers/nobody@example.com").status_code == 404


def test_maintainer_view_404_malformed_address(client):
    """A URL segment that fails the address regex 404s before ever
    touching the DB."""
    assert client.get("/maintainers/not-an-email").status_code == 404


def test_maintainer_view_canonical_link_is_lowercased(client, session):
    """The `<link rel="canonical">` uses the lowercased address even
    when the stored MAINTAINERS casing (and the request URL) differ."""
    import re

    _add_subsystem(
        session,
        "ADVANTECH",
        "Maintained",
        maintainers=[("M", "Foo Bar", "Foo.Bar@x.org")],
    )

    body = client.get("/maintainers/Foo.Bar@x.org").data.decode()
    m = re.search(r'<link rel="canonical" href="([^"]+)"', body)
    assert m is not None
    assert m.group(1).endswith("/maintainers/foo.bar@x.org")


def test_maintainer_view_emits_profilepage_json_ld(client, session):
    """`/maintainers/<address>` ships a minimal `ProfilePage` payload
    with a `Person` mainEntity."""
    _add_subsystem(
        session,
        "BCACHEFS",
        "Supported",
        maintainers=[("M", "Kent Overstreet", "kent@kernel.org")],
    )

    blocks = _json_ld_blocks(client.get("/maintainers/kent@kernel.org").data.decode())
    assert len(blocks) == 1
    payload = blocks[0]
    assert payload["@type"] == "ProfilePage"
    assert "Kent Overstreet" in payload["name"]
    assert payload["mainEntity"]["@type"] == "Person"
    assert payload["mainEntity"]["name"] == "Kent Overstreet"
    assert payload["url"].endswith("/maintainers/kent@kernel.org")


@pytest.mark.parametrize("surface", ["thread", "metadata"])
def test_message_metadata_does_not_link_reviewers_to_maintainer_profiles(
    client, tmp_path, surface
):
    """`/maintainers/<address>` is an M-only surface, and the message
    metadata's subsystem header also linkifies a maintainer. The thread
    summary and per-message fragment must keep the same link predicate.

    The sibling guard on the subsystem page
    (`test_subsystem_page_does_not_link_reviewer_addresses_to_maintainer_profiles`)
    had no counterpart here, so the `_role == 'M'` filter in
    `_subsystem_link.html` is what stands between an `R:`
    reviewer and a linked 404, with nothing holding it.

    The allowlist gate the template also applies does NOT stand in for
    the role check: the allowlist is the UNION of `M:` and `R:`, so
    every reviewer passes it. It answers "is this address safe to
    display", not "does this profile exist".
    """
    from tests.test_routes._helpers import (
        _ingest_one_article,
        _metadata_url,
        _seed_subsystem,
    )

    _seed_subsystem(
        "BCACHEFS",
        "Maintained",
        files=["fs/bcachefs/"],
        maintainers=[
            ("M", "Kent Overstreet", "kent.overstreet@kernel.org"),
            ("R", "Reviewer Person", "reviewer.only@kernel.org"),
        ],
    )
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "msgpage-roles@example.com",
        body=b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n",
    )
    target = url + "/t" if surface == "thread" else _metadata_url(url)
    body = client.get(target).get_data(as_text=True)

    assert 'href="/maintainers/kent.overstreet@kernel.org"' in body
    assert 'href="/maintainers/reviewer.only@kernel.org"' not in body
    # The gate would have passed the reviewer: it is allowlisted.
    from mimir.web.filters import _is_allowlisted_address_filter

    with client.application.test_request_context("/"):
        assert _is_allowlisted_address_filter("reviewer.only@kernel.org") is True
    # And the thing that actually matters: nothing linked from here 404s.
    assert client.get("/maintainers/kent.overstreet@kernel.org").status_code == 200
    assert client.get("/maintainers/reviewer.only@kernel.org").status_code == 404


def test_every_url_the_maintainers_sitemap_advertises_resolves(client, session):
    """Every `<loc>` in `/sitemap-maintainers.xml` must be a page.

    Supported punctuation and mixed-case addresses survive URL encoding
    and resolve through the real route.
    """
    from tests.test_routes._helpers import _clear_sitemap_cache

    _add_subsystem(
        session,
        "SHAPES",
        "Maintained",
        maintainers=[
            # Every punctuation class the regex admits, plus the
            # mixed-case form MAINTAINERS really uses.
            ("M", "Dot Name", "first.last@vger.kernel.org"),
            ("M", "Plus Tag", "someone+kernel@example.co.uk"),
            ("M", "Dashed Host", "dev@my-host.example.com"),
            ("M", "Under Score", "some_one@example.com"),
            ("M", "Mixed Case", "Andrea.Ho@Advantech.COM.TW"),
        ],
    )
    _clear_sitemap_cache()

    xml = client.get("/sitemap-maintainers.xml").get_data(as_text=True)
    locs = re.findall(r"<loc>([^<]+)</loc>", xml)
    assert len(locs) == 5, f"expected one urlset entry per maintainer, got {locs}"

    for loc in locs:
        path = re.sub(r"^https?://[^/]+", "", loc)
        assert client.get(path).status_code == 200, (
            f"sitemap advertises {loc!r} but the route refuses it; the "
            "URL builder and the route's validity rule have drifted"
        )


def test_maintainer_link_url_requires_the_allowlist(monkeypatch):
    """An address the page would redact gets no profile link. Seeded
    maintainers are always allowlisted, so route tests cannot reach
    this; it bites on a deploy whose MAINTAINERS-derived set is empty."""
    from mimir import maintainer_allowlist
    from mimir.web.filters import _maintainer_link_url_filter

    address = "kent@example.com"
    monkeypatch.setattr(maintainer_allowlist, "maintainer_addresses", frozenset)
    assert _maintainer_link_url_filter(address) == ""

    monkeypatch.setattr(
        maintainer_allowlist, "maintainer_addresses", lambda: frozenset({address})
    )
    assert _maintainer_link_url_filter(address) == "/maintainers/kent@example.com"


@pytest.mark.parametrize("surface", ["sitemap", "subsystem", "thread", "metadata"])
def test_maintainer_links_skip_unaddressable_profiles(client, tmp_path, surface):
    """Removing any emitter's validity gate publishes a seeded 404."""
    from urllib.parse import quote, urlsplit
    from xml.etree import ElementTree

    from mimir.web.filters import _is_allowlisted_address_filter
    from tests.test_routes._helpers import (
        _clear_sitemap_cache,
        _ingest_one_article,
        _metadata_url,
        _seed_subsystem,
    )

    unsupported = [
        "o'brien@kernel.org",
        "jörg@kernel.org",
        "JÖRGEN@kernel.org",
        "slash/name@kernel.org",
        "tab\tname@kernel.org",
        "newline@kernel.org\n",
        "",
    ]
    _seed_subsystem(
        "SHAPES",
        "Maintained",
        files=["fs/shapes/"],
        maintainers=[("M", "Valid Maintainer", "Mixed+tag%name@kernel.org")]
        + [("M", f"Unsupported {i}", addr) for i, addr in enumerate(unsupported)],
    )
    with client.application.test_request_context("/"):
        for addr in unsupported[:-1]:
            assert _is_allowlisted_address_filter(addr), addr

    if surface == "sitemap":
        _clear_sitemap_cache()
        response = client.get("/sitemap-maintainers.xml")
        assert response.status_code == 200
        urls = [
            node.text
            for node in ElementTree.fromstring(response.data).findall("{*}url/{*}loc")
        ]
    else:
        url = "/alpha/subsystem/shapes/"
        if surface in {"thread", "metadata"}:
            _, url = _ingest_one_article(
                tmp_path,
                "alpha",
                "address-shapes@example.com",
                body=b"diff --git a/fs/shapes/a.c b/fs/shapes/a.c\n@@ -1 +1 @@\n-x\n+y\n",
            )
            url = url + "/t" if surface == "thread" else _metadata_url(url)
        response = client.get(url)
        assert response.status_code == 200

        html = response.get_data(as_text=True)
        urls = re.findall(r'href="(/maintainers/[^"]*)"', html)
        for i in range(len(unsupported)):
            assert f"Unsupported {i}" in html

    paths = [urlsplit(url).path for url in urls]
    assert paths == ["/maintainers/mixed%2Btag%25name@kernel.org"]
    assert client.get(paths[0]).status_code == 200
    for addr in unsupported:
        assert client.get("/maintainers/" + quote(addr, safe="@")).status_code == 404


def _profile_panel(body):
    from xml.etree import ElementTree as ET

    match = re.search(
        r'<section class="maintainer-profile">.*?</section>', body, re.DOTALL
    )
    assert match is not None, "expected the maintainer profile panel"
    return ET.fromstring(match.group())


def test_profile_header_cards_and_alphabetical_cold_review_chips(client, session):
    from flask import url_for

    _add_subsystem(
        session, "ZFS", "Maintained", [("M", "Ada Example", "ada@kernel.org")]
    )
    _add_subsystem(
        session, "BCACHEFS", "Supported", [("M", "Ada Example", "ada@kernel.org")]
    )
    _add_trailer(session, "art2@example.com", "ada@kernel.org")
    _add_trailer(session, "art1@example.com", "ada@kernel.org")
    response = client.get("/maintainers/Ada@Kernel.org")
    assert response.status_code == 200
    panel = _profile_panel(response.get_data(as_text=True))
    avatar = panel.find("./header/span")
    assert avatar.text == "AE"
    assert avatar.attrib["aria-hidden"] == "true"
    assert panel.find("./header/div/h1").text == "Ada Example"
    assert "Maintainer of 2 subsystems" in "".join(panel.itertext())
    assert "Reviews seen in 2 inboxes" in "".join(panel.itertext())
    cards = panel.findall("./ul[@class='maintainer-subsystems']/li")
    assert [c.find("strong").text for c in cards] == ["bcachefs", "zfs"]
    assert [c.find("span").text for c in cards] == ["Supported", "Maintained"]
    assert all(c.find("a") is None for c in cards)
    chips = panel.findall("./ul[@class='maintainer-review-chips']/li/a")
    assert [a.find("span").text for a in chips] == ["alpha", "beta"]
    assert all(a.find("strong") is None for a in chips)
    for chip in chips:
        with client.application.test_request_context():
            expected = url_for(
                "web.reviewer_view",
                inbox_name=chip.find("span").text,
                address="ada@kernel.org",
            )
        assert chip.attrib["href"] == expected
        assert client.get(chip.attrib["href"]).status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=600"
    assert "ETag" not in response.headers
    assert panel.findall(".//*[@style]") == []


def test_profile_safe_display_and_missing_name_initials(client, session):
    _add_subsystem(
        session, "DRIVERS", None, [("M", "private@example.com", "ada@kernel.org")]
    )
    panel = _profile_panel(
        client.get("/maintainers/ada@kernel.org").get_data(as_text=True)
    )
    assert panel.find("./header/div/h1").text == "ada@kernel.org"
    assert panel.find("./header/span").text == "A"
    assert "private@example.com" not in "".join(panel.itertext())
    assert "No indexed review activity yet." in "".join(panel.itertext())
    assert panel.findall("./ul[@class='maintainer-subsystems']/li/span") == []


def test_profile_counts_use_one_snapshot_and_never_aggregate_in_requests(
    client, session, monkeypatch
):
    from mimir import cache
    from mimir import maintainer_directory as directory

    address = "ada@kernel.org"
    _add_subsystem(session, "FILES", "Maintained", [("M", "Ada Example", address)])
    _add_trailer(session, "art1@example.com", address)
    directory.maintainer_profile(session, address)  # Older profile knows only alpha.
    _add_trailer(session, "art2@example.com", address)
    _add_trailer(session, "art2@example.com", address, role="Tested-by")
    directory.warm_maintainer_review_counts(session)

    def unexpected(*args, **kwargs):
        raise AssertionError("profile request aggregated trailer counts")

    monkeypatch.setattr(directory, "maintainer_review_counts_query", unexpected)

    def panel():
        return _profile_panel(
            client.get("/maintainers/Ada@Kernel.org").get_data(as_text=True)
        )

    warm = panel()
    chips = warm.findall("./ul[@class='maintainer-review-chips']/li/a")
    assert [(c.findtext("span"), c.findtext("strong")) for c in chips] == [
        ("beta", "2"),
        ("alpha", "1"),
    ]
    assert "Reviews seen in 2 inboxes" in "".join(warm.itertext())
    for chip in chips:
        assert client.get(chip.attrib["href"]).status_code == 200

    key = "maintainer:review_counts_v1:" + address
    cache.set(key, {"beta": 2, "alpha": 2}, 3600)
    assert [
        c.findtext("span")
        for c in panel().findall("./ul[@class='maintainer-review-chips']/li/a")
    ] == ["alpha", "beta"]
    cache.set(key, {"beta": 2}, 3600)
    reduced = panel()
    assert "Reviews seen in 1 inbox" in "".join(reduced.itertext())
    assert [
        c.findtext("span")
        for c in reduced.findall("./ul[@class='maintainer-review-chips']/li/a")
    ] == ["beta"]
    cache.set(key, {}, 3600)
    empty = panel()
    assert "Reviews seen in 0 inboxes" in "".join(empty.itertext())
    assert "No indexed review activity yet." in "".join(empty.itertext())
    cache.set(key, {"beta": 2}, -1)
    cold = panel()
    chips = cold.findall("./ul[@class='maintainer-review-chips']/li/a")
    assert [c.findtext("span") for c in chips] == ["alpha"]
    assert all(c.find("strong") is None for c in chips)

    cache.set(key, {"beta": 2}, 3600)
    owner = session.execute(
        select(SubsystemMaintainer).where(SubsystemMaintainer.address == address)
    ).scalar_one()
    owner.role = "R"
    session.commit()
    assert client.get("/maintainers/ada@kernel.org").status_code == 404
