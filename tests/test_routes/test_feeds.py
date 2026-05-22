"""Tests for mimir/web/routes/feeds.py: per-inbox, per-author,
and per-reviewer Atom feeds plus autodiscovery `<link>`
tags wired into base.html."""

from tests.test_routes._helpers import _ingest_one_article, _seed_author_article


def test_author_feed_too_short_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/author/x/feed.atom").status_code == 404


def test_author_page_autodiscovery_link(client, inbox_name):
    """The per-author HTML page advertises its atom feed via
    `<link rel="alternate" type="application/atom+xml">` in the head,
    so feed readers can subscribe from the page URL alone."""
    r = client.get(f"/{inbox_name}/author/example.com")
    assert r.status_code == 200
    body = r.data.decode()
    # The autodiscovery contract is *one specific <link>* element
    # carrying all three attrs together. Separate string searches
    # would pass even if rel="alternate" and the Atom feed lived
    # on different elements. Match the whole element instead.
    import re

    link_re = re.compile(
        r'<link\b[^>]*rel="alternate"[^>]*type="application/atom\+xml"[^>]*'
        r'href="/' + re.escape(inbox_name) + r'/author/example\.com/feed\.atom"',
        re.DOTALL,
    )
    assert link_re.search(body) is not None, (
        f"missing <link rel='alternate' type='application/atom+xml' "
        f"href='/{inbox_name}/author/example.com/feed.atom'> in <head>"
    )


def test_inbox_tracker_tile_links_to_atom(client, inbox_name):
    """The tracker tiles on the inbox dashboard surface a small `atom`
    link next to `all →` so the per-author feed is discoverable
    without visiting the HTML view first."""
    from mimir.inboxes import set_tracked_authors

    # Seed a tracker on the inbox so the tile section renders.
    set_tracked_authors(inbox_name, {"Examples": "example.com"})
    r = client.get(f"/{inbox_name}/")
    assert r.status_code == 200
    body = r.data.decode()
    assert f"/{inbox_name}/author/example.com/feed.atom" in body


def test_atom_feed_well_formed(client, inbox_name):
    """The Atom feed must:
    - parse as XML with the Atom namespace,
    - carry required <id>, <title>, <updated> at the feed level,
    - carry at least one <entry> (the seeded inbox has articles),
    - and each entry must have <id>, <title>, <updated>, plus a
      <link rel="alternate"> pointing back into the archive.

    A feed missing entries -- which the older presence-only test
    would've passed -- is useless to feed readers."""
    import re
    import xml.etree.ElementTree as ET

    r = client.get(f"/{inbox_name}/feed.atom")
    assert r.status_code == 200
    assert r.mimetype.startswith("application/atom+xml")
    root = ET.fromstring(r.get_data())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    assert root.tag.endswith("feed")  # root is <feed>
    for required in ("a:id", "a:title", "a:updated"):
        assert root.find(required, ns) is not None, f"feed-level {required} missing"

    entries = root.findall("a:entry", ns)
    assert len(entries) > 0, "feed has no <entry> elements"
    for e in entries:
        assert e.find("a:id", ns) is not None
        assert e.find("a:title", ns) is not None
        assert e.find("a:updated", ns) is not None
        # Each entry has an alternate link pointing at the article URL.
        alt = e.find("a:link[@rel='alternate']", ns)
        assert alt is not None and alt.get("href"), (
            "entry missing rel=alternate link with href"
        )
        # The href must be under the inbox we're querying. Cross-post
        # canonical resolution flips it to a sibling inbox; both
        # `/<inbox>/...` and `/<sibling>/...` are valid, but the host
        # mustn't leak (only path) and the path must be one of mimir's
        # message URLs.
        href = alt.get("href")
        # Path-shape match rather than a hardcoded year disjunction
        # the latter goes stale every January and gives no diagnostic
        # value beyond "the URL contains some year we listed."
        assert re.search(r"/\d{4}/\d{2}/\d+(?:[/?#]|$)", href), (
            f"entry link doesn't look like a message URL: {href}"
        )


def test_atom_feed_author_name_is_display_name_only(
    client,
    tmp_path,
    monkeypatch,
):
    """Atom <author><name> is always the display name only -- the
    `<hidden>` placeholder reads as broken metadata in feed readers
    exactly as it did in JSON-LD before the 2026-05-12 fix. The
    address rides along separately in <author><email>, gated on the
    allowlist (see test_atom_feed_author_includes_email_when_allowlisted)."""
    import xml.etree.ElementTree as ET
    from mimir.config import settings

    monkeypatch.setattr(settings, "email_allowlist", [])
    _ingest_one_article(
        tmp_path,
        "alpha",
        "atom-named@example.com",
        author="David Woodhouse <dwmw2@infradead.org>",
    )
    r = client.get("/alpha/feed.atom")
    assert r.status_code == 200
    root = ET.fromstring(r.get_data())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    names = [
        e.findtext("a:author/a:name", default="", namespaces=ns)
        for e in root.findall("a:entry", ns)
    ]
    target = next(n for n in names if "David Woodhouse" in n)
    assert target == "David Woodhouse"
    # Belt-and-braces: across all entries, no placeholder, no `@`
    # in any byline (<name> is name-only; addresses ride in <email>).
    # The seeded `a@b.example` corpus has no display name and falls
    # through to the neutral fallback rather than leaking the bare
    # address.
    for n in names:
        assert "<hidden>" not in n
        assert "@" not in n


def test_atom_feed_author_includes_email_when_allowlisted(
    client,
    tmp_path,
    monkeypatch,
):
    """Atom <author><email> is present iff the sender is in the
    allowlist union (same gate `_safe_from_filter` uses on the
    visible HTML side). The address is already on the rendered
    page and in the public git blob, omitting it from the feed
    under-attributes the only set of senders we don't redact."""
    import xml.etree.ElementTree as ET
    from mimir.config import settings

    monkeypatch.setattr(settings, "email_allowlist", ["@b.example"])
    _ingest_one_article(
        tmp_path,
        "alpha",
        "atom-allow@example.com",
        author="Allowed Person <allowed@b.example>",
    )
    r = client.get("/alpha/feed.atom")
    assert r.status_code == 200
    root = ET.fromstring(r.get_data())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = next(
        e
        for e in root.findall("a:entry", ns)
        if e.findtext("a:author/a:name", default="", namespaces=ns) == "Allowed Person"
    )
    assert entry.findtext("a:author/a:email", namespaces=ns) == "allowed@b.example"


def test_atom_feed_author_omits_email_when_not_allowlisted(
    client,
    tmp_path,
    monkeypatch,
):
    """Inverse: a non-allowlisted sender's address stays out of the
    feed entirely, matching the visible HTML's `<hidden>` redaction."""
    import xml.etree.ElementTree as ET
    from mimir.config import settings

    monkeypatch.setattr(settings, "email_allowlist", [])
    _ingest_one_article(
        tmp_path,
        "alpha",
        "atom-hide@example.com",
        author="Casual Sender <casual@example.org>",
    )
    r = client.get("/alpha/feed.atom")
    assert r.status_code == 200
    root = ET.fromstring(r.get_data())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = next(
        e
        for e in root.findall("a:entry", ns)
        if e.findtext("a:author/a:name", default="", namespaces=ns) == "Casual Sender"
    )
    assert entry.find("a:author/a:email", namespaces=ns) is None


# robots.txt / security.txt / sitemap.xml


def test_atom_feed_cross_post_id_is_canonical_in_either_feed(client):
    """The same cross-posted article surfaces in both /alpha/feed.atom
    and /beta/feed.atom. With canonical resolution, both feeds' entries
    for art3 must carry the SAME <id>, the canonical URL, so feed
    readers that key on <id> deduplicate across feeds."""
    import xml.etree.ElementTree as ET
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        art3 = s.execute(
            select(Article).where(Article.message_id == "art3@example.com")
        ).scalar_one()
        art_id = art3.id

    ns = {"a": "http://www.w3.org/2005/Atom"}

    def find_id_for(feed_xml: bytes, art_id: int) -> str | None:
        root = ET.fromstring(feed_xml)
        for entry in root.findall("a:entry", ns):
            link = entry.find("a:link[@rel='alternate']", ns)
            if link is not None and link.get("href", "").endswith(f"/{art_id}"):
                id_el = entry.find("a:id", ns)
                return id_el.text if id_el is not None else None
        return None

    alpha_feed = client.get("/alpha/feed.atom")
    beta_feed = client.get("/beta/feed.atom")
    assert alpha_feed.status_code == 200
    assert beta_feed.status_code == 200

    alpha_id = find_id_for(alpha_feed.get_data(), art_id)
    beta_id = find_id_for(beta_feed.get_data(), art_id)
    assert alpha_id is not None and beta_id is not None
    assert alpha_id == beta_id
    # Fallback is alphabetical-first → alpha.
    assert "alpha" in alpha_id


# Per-page title tags + sitemap <lastmod>


def test_author_feed_is_well_formed_atom_with_matching_entry(
    client,
    inbox_name,
):
    """Atom feed for one author: must parse as Atom, carry the
    correct feed-level title for the substring, and include the
    seeded article as an entry."""
    import xml.etree.ElementTree as ET

    _seed_author_article(
        inbox_name,
        author="Atomic Person <atomic-77@example.org>",
        message_id="author-feed@example.com",
    )
    r = client.get(f"/{inbox_name}/author/atomic-77/feed.atom")
    assert r.status_code == 200
    assert "atom" in r.mimetype or r.mimetype == "application/atom+xml"

    root = ET.fromstring(r.data)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    assert root.tag.endswith("feed")

    feed_title = root.find("a:title", ns)
    assert feed_title is not None
    assert "atomic-77" in (feed_title.text or "")

    entries = root.findall("a:entry", ns)
    assert len(entries) >= 1
    entry_titles = [e.find("a:title", ns).text or "" for e in entries]
    assert any("author route subject" in t for t in entry_titles)


def test_author_feed_too_short_substring_404s(client, inbox_name):
    assert client.get(f"/{inbox_name}/author/a/feed.atom").status_code == 404


# Per-reviewer page (`/<inbox>/reviewer/<address>`), slice 3 of #97.
