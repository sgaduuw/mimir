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
    assert "BCACHEFS" in body
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


# The emitter/acceptor pair. `maintainer_path` (used by the two
# template link sites AND by /sitemap-maintainers.xml) produces the
# address; `maintainer_view` decides whether to serve it. Nothing in
# the code forces the two to agree, so the agreement is pinned here
# rather than asserted in prose. See CONTEXT.md "Emitter and acceptor
# must share one validity rule".


def test_message_page_does_not_link_reviewer_addresses_to_maintainer_profiles(
    client, tmp_path
):
    """`/maintainers/<address>` is an M-only surface, and the message
    page's subsystem header is the second place that linkifies a
    maintainer.

    The sibling guard on the subsystem page
    (`test_subsystem_page_does_not_link_reviewer_addresses_to_maintainer_profiles`)
    had no counterpart here, so the `_role == 'M'` filter in
    `_message_body.html` was the only thing standing between an `R:`
    reviewer and a linked 404, with nothing holding it.

    The allowlist gate the template also applies does NOT stand in for
    the role check: the allowlist is the UNION of `M:` and `R:`, so
    every reviewer passes it. It answers "is this address safe to
    display", not "does this profile exist".
    """
    from tests.test_routes._helpers import _ingest_one_article, _seed_subsystem

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
    body = client.get(url).get_data(as_text=True)

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

    The sitemap builds its URLs with `maintainer_path` and applies no
    addressability predicate (unlike subsystem names, which go through
    `subsystems.is_addressable_subsystem_name`), while the route gates
    on `_MAINTAINER_ADDR_RE`. Production carried 2,467 distinct
    (role, address) pairs on 2026-08-03 and all of them satisfy that
    regex, so the two agree today; this pins that they keep agreeing
    for the address shapes MAINTAINERS actually contains.
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP (latent, zero instances on production as of "
        "2026-08-03): `maintainer_path` percent-encodes any address at "
        "all, while `maintainer_view` rejects anything outside "
        "`_MAINTAINER_ADDR_RE`. `maintainers._split_addr` stores "
        "whatever sits between the last '<' and '>' of an `M:` line, so "
        "one apostrophe or one non-ASCII character upstream publishes a "
        "404 to every crawler via /sitemap-maintainers.xml. The fix is "
        "a shared addressability predicate, the same shape as "
        "`subsystems.is_addressable_subsystem_name`; remove this xfail "
        "when it lands."
    ),
)
def test_maintainer_path_never_emits_a_url_the_route_refuses(client, session):
    """An apostrophe is legal in an email local part, legal in
    MAINTAINERS, and accepted by mimir's parser. It should not be
    possible to emit a link and a sitemap entry for an address the
    route will not serve."""
    from mimir.maintainer_directory import maintainer_path

    _add_subsystem(
        session,
        "APOSTROPHE",
        "Maintained",
        maintainers=[("M", "Sean O'Brien", "o'brien@example.com")],
    )
    assert client.get(maintainer_path("o'brien@example.com")).status_code == 200
