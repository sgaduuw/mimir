"""Tests for mimir/web/routes/static_meta.py: robots.txt,
security.txt, favicon.svg, og-image.png, theme-color,
generator meta, and the OG / Twitter Card / canonical /
alternate-feed `<link>` tags wired into base.html."""

from tests.test_routes._helpers import _ingest_one_article, _meta_value


def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert r.mimetype == "text/plain"
    body = r.get_data(as_text=True)
    assert "User-agent: *" in body
    assert "Sitemap:" in body
    assert "/sitemap.xml" in body


def test_robots_txt_reflects_admin_added_rule(client):
    """A row added via the service layer surfaces on the next
    /robots.txt request. Pins the DB-backed render path."""
    from mimir import robots

    robots.add_rule("GPTBot", disallow=["/"])
    body = client.get("/robots.txt").get_data(as_text=True)
    assert "User-agent: GPTBot" in body
    assert "Disallow: /" in body


def test_robots_txt_reset_restores_default_body(client):
    """After `reset_rules()`, the file matches the migration's
    seeded `*` stanza only."""
    from mimir import robots

    robots.add_rule("GPTBot", disallow=["/"])
    robots.reset_rules()
    body = client.get("/robots.txt").get_data(as_text=True)
    assert "GPTBot" not in body
    assert "User-agent: *" in body
    assert "Crawl-delay: 5" in body
    assert "Disallow: /*/attachment/" in body


def test_security_txt_404_when_unconfigured(client, monkeypatch):
    from mimir.config import settings

    monkeypatch.setattr(settings, "security_contact", None)
    assert client.get("/security.txt").status_code == 404
    assert client.get("/.well-known/security.txt").status_code == 404


def test_security_txt_when_configured(client, monkeypatch):
    from mimir.config import settings

    monkeypatch.setattr(settings, "security_contact", "mailto:test@example.com")
    monkeypatch.setattr(settings, "security_policy_url", None)
    monkeypatch.setattr(settings, "security_encryption_url", None)
    for url in ("/security.txt", "/.well-known/security.txt"):
        r = client.get(url)
        assert r.status_code == 200
        assert r.mimetype == "text/plain"
        body = r.get_data(as_text=True)
        assert "Contact: mailto:test@example.com" in body
        # Expires is dynamic; just confirm the field is present.
        assert "Expires:" in body
        assert "Preferred-Languages: en" in body


def test_security_txt_optional_fields_emitted_when_set(client, monkeypatch):
    from mimir.config import settings

    monkeypatch.setattr(settings, "security_contact", "mailto:test@example.com")
    monkeypatch.setattr(settings, "security_policy_url", "https://example.com/policy")
    monkeypatch.setattr(
        settings, "security_encryption_url", "https://example.com/pgp.asc"
    )
    body = client.get("/security.txt").get_data(as_text=True)
    assert "Policy: https://example.com/policy" in body
    assert "Encryption: https://example.com/pgp.asc" in body


def test_og_tags_on_index_match_expected_values(client):
    html = client.get("/").data.decode()
    expected_desc = (
        "Linux kernel mailing list archives. ~200 inboxes indexed with "
        "cross-list deduplication, subsystem dashboards, patch-series "
        "timelines, and reviewer activity surfaces."
    )
    assert _meta_value(html, "og:title") == "indexed mailing list archives | mimir"
    assert _meta_value(html, "og:type") == "website"
    assert _meta_value(html, "og:site_name") == "mimir"
    assert _meta_value(html, "og:url") == "http://localhost/"
    assert _meta_value(html, "og:description") == expected_desc


def test_og_type_article_on_message_page(client, tmp_path):
    """Message pages override og:type to 'article' (the rest of the
    site stays on the 'website' default). End-to-end via a real
    ingested article so we exercise the rendered tag, not the
    template source."""
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "ogtype@example.com",
    )
    r = client.get(url)
    assert r.status_code == 200
    assert _meta_value(r.data.decode(), "og:type") == "article"


def test_twitter_card_tags_match_og_pair(client, inbox_name):
    """twitter:title/description should mirror og:title/description so
    Twitter's preview matches whatever Slack/etc. show via OG. The
    card type is `summary_large_image` paired with the 1200x630 PNG
    og:image (Twitter/X doesn't render SVG, LinkedIn is inconsistent
    on it -- flagged in the 2026-05-12 review)."""
    html = client.get(f"/{inbox_name}/").data.decode()
    assert _meta_value(html, "twitter:card") == "summary_large_image"
    assert _meta_value(html, "twitter:title") == _meta_value(html, "og:title")
    assert _meta_value(html, "twitter:description") == _meta_value(
        html, "og:description"
    )
    assert _meta_value(html, "twitter:title") == f"{inbox_name} | mimir"
    # Image mirrors og:image; the asset itself is a 1200x630 PNG.
    assert _meta_value(html, "twitter:image") == _meta_value(html, "og:image")
    assert _meta_value(html, "og:image").endswith("/og-image.png")
    # Width / height / alt help picky link-card renderers pick the
    # intended size and improve a11y. Mirrored on twitter:image:alt.
    assert _meta_value(html, "og:image:width") == "1200"
    assert _meta_value(html, "og:image:height") == "630"
    og_alt = _meta_value(html, "og:image:alt") or ""
    assert "Ratatoskr" in og_alt
    assert _meta_value(html, "twitter:image:alt") == og_alt


def test_meta_description_inbox_uses_rich_form_when_stats_present(client):
    """Seeded alpha has 3 linked articles → stats.total>0 → description
    is the rich form: '<inbox> archive: N message(s) from <first> to
    <last>, indexed and searchable.' (NOT the bare fallback '<inbox>
    archive on <site>.')"""
    html = client.get("/alpha/").data.decode()
    desc = _meta_value(html, "description")
    assert desc is not None
    # Markers exclusive to the rich form.
    assert desc.startswith("alpha archive: ")
    assert "message" in desc
    assert "indexed and searchable" in desc


# JSON-LD structured data


def test_canonical_link_on_homepage(client):
    """Every emitted page carries a canonical. Pre-meta-sweep the
    homepage was missing one; now it pins itself."""
    html = client.get("/").data.decode()
    import re as _re

    m = _re.search(r'<link rel="canonical" href="([^"]+)"', html)
    assert m is not None
    href = m.group(1)
    assert href.endswith("/")
    assert href.startswith("http://") or href.startswith("https://")


def test_canonical_link_on_inbox_dashboard(client, inbox_name):
    html = client.get(f"/{inbox_name}/").data.decode()
    import re as _re

    m = _re.search(r'<link rel="canonical" href="([^"]+)"', html)
    assert m is not None
    assert m.group(1).endswith(f"/{inbox_name}/")


def test_favicon_svg_served(client):
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.mimetype == "image/svg+xml"
    assert b"<svg" in r.data


def test_og_image_png_served(client):
    """The OG image is a pre-baked 1200x630 PNG (no longer an SVG
    rendered from a template -- Twitter/X doesn't render SVG and
    LinkedIn is inconsistent on it; flagged in the 2026-05-12
    review). The asset lives at `mimir/static/img/og-image.png` and
    is served from the site root for URL-shape continuity with the
    prior `/og-image.svg`. The route is bare bytes; we assert the
    PNG signature and the dimensions reflect the baked composite."""
    r = client.get("/og-image.png")
    assert r.status_code == 200
    assert r.mimetype == "image/png"
    # PNG magic bytes, defends against the file being silently
    # replaced by something else with a `.png` name.
    assert r.data[:8] == b"\x89PNG\r\n\x1a\n"
    # IHDR chunk holds width/height in the first 8 bytes after the
    # length+type prefix (16..24). Pin the dimensions; a re-bake at
    # a different size would invalidate the og:image:width/height
    # meta tags emitted in base.html.
    import struct

    width, height = struct.unpack(">II", r.data[16:24])
    assert (width, height) == (1200, 630)


def test_robots_txt_disallows_api_and_search_surfaces(client):
    """`/api/` (htmx partials, one URL per offset) and `/*/search`
    (internal search results, one URL per query) are crawl-budget
    sinks that can never be a useful search result. Pins them in the
    default `*` stanza so a future edit to the defaults can't quietly
    drop them."""
    body = client.get("/robots.txt").get_data(as_text=True)
    assert "Disallow: /api/" in body
    assert "Disallow: /*/search" in body
    # The pre-existing attachment rule must survive the addition.
    assert "Disallow: /*/attachment/" in body


def test_since_view_is_noindex_and_omits_canonical(client, tmp_path):
    """The "what I missed" window is ~90 near-duplicate URLs per inbox
    over overlapping thread sets, so it carries `noindex`. base.html
    suppresses the canonical link when noindex is set (a canonical on a
    page we're asking not to index is a mixed signal). `noindex` alone
    implies follow, so the outbound thread links stay a crawl path."""
    _ingest_one_article(tmp_path, "alpha", "since-noindex@example.com")
    html = client.get("/alpha/since/2026-01-01").get_data(as_text=True)
    assert '<meta name="robots" content="noindex">' in html
    assert '<link rel="canonical"' not in html
