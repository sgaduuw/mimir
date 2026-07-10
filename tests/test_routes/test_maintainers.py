"""Tests for `/maintainers/<address>`: the global (cross-inbox)
maintainer profile page. See mimir/web/routes/maintainers.py and
mimir/templates/maintainer.html."""

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
