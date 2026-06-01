"""Tests for mimir/web/routes/sitemaps.py: the meta-sitemap,
per-inbox sitemap, `<lastmod>` correctness, cache invalidation
after canonical-inbox flips."""

from tests.test_routes._helpers import _clear_sitemap_cache


def test_sitemap_xml(client):
    """`/sitemap.xml` is the sitemap index: a `<sitemapindex>` of
    `<sitemap>` children, one per inbox sub-sitemap plus `/meta-sitemap.xml`.
    Every linked sub-sitemap URL must itself resolve (200) -- a
    crawler that follows a 404 sitemap link reports the site as
    broken even if `/sitemap.xml` itself rendered fine."""
    import xml.etree.ElementTree as ET
    from urllib.parse import urlparse

    _clear_sitemap_cache()
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.mimetype == "application/xml"
    root = ET.fromstring(r.get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    # Root element is <sitemapindex>, not <urlset>.
    assert root.tag.endswith("sitemapindex")
    sitemaps = root.findall("s:sitemap", ns)
    locs = {sm.find("s:loc", ns).text for sm in sitemaps}
    # Every configured inbox gets a sub-sitemap. A future regression
    # that drops an inbox from the iteration would silently leave it
    # un-indexed.
    expected_sub_sitemaps = {
        "/meta-sitemap.xml",
        "/alpha/sitemap.xml",
        "/beta/sitemap.xml",
    }
    found_paths = {urlparse(loc).path for loc in locs}
    assert expected_sub_sitemaps.issubset(found_paths), (
        f"missing sub-sitemap(s): {expected_sub_sitemaps - found_paths}"
    )
    # Every advertised sub-sitemap URL actually resolves (within mimir's
    # own routes). Hit each path and check 200 + parseable XML.
    for loc in locs:
        path = urlparse(loc).path
        sub_r = client.get(path)
        assert sub_r.status_code == 200, (
            f"sitemap index lists {path!r} which returned {sub_r.status_code}"
        )
        ET.fromstring(sub_r.get_data())  # well-formed XML


# ProxyFix: trusted_proxy_hops controls X-Forwarded-* honouring


def test_sitemap_cross_post_appears_in_each_linked_inbox(client):
    """art3 is cross-posted alpha+beta. In the per-inbox sitemap world,
    it is listed under *both* `/alpha/sitemap.xml` and
    `/beta/sitemap.xml`, each one is a real, crawlable URL, and the
    canonical `<link>` on the page itself tells search engines which
    to keep. The sitemap doesn't try to enforce one-canonical-URL-per-
    article anymore (that was the old global-sitemap design)."""
    import xml.etree.ElementTree as ET
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        art3 = s.execute(
            select(Article).where(Article.message_id == "art3@example.com")
        ).scalar_one()
        art_id = art3.id

    _clear_sitemap_cache()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    def article_locs(inbox: str) -> list[str]:
        root = ET.fromstring(client.get(f"/{inbox}/sitemap.xml").get_data())
        return [
            u.find("s:loc", ns).text
            for u in root.findall("s:url", ns)
            if u.find("s:loc", ns).text.endswith(f"/{art_id}")
        ]

    alpha_locs = article_locs("alpha")
    beta_locs = article_locs("beta")
    assert len(alpha_locs) == 1 and "/alpha/" in alpha_locs[0]
    assert len(beta_locs) == 1 and "/beta/" in beta_locs[0]


def test_sitemap_index_sub_sitemap_lastmods(client):
    """The sitemap index emits a `<lastmod>` per sub-sitemap so
    crawlers can skip unchanged inboxes. With seeded data, alpha,
    beta, and the meta-sitemap all carry the global max 2024-03-01
    (art3's cross-post date)."""
    import xml.etree.ElementTree as ET

    _clear_sitemap_cache()
    r = client.get("/sitemap.xml")
    root = ET.fromstring(r.get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    lastmods = {
        sm.find("s:loc", ns).text: (
            sm.find("s:lastmod", ns).text
            if sm.find("s:lastmod", ns) is not None
            else None
        )
        for sm in root.findall("s:sitemap", ns)
    }
    meta_loc = next(loc for loc in lastmods if loc.endswith("/meta-sitemap.xml"))
    alpha_loc = next(loc for loc in lastmods if loc.endswith("/alpha/sitemap.xml"))
    beta_loc = next(loc for loc in lastmods if loc.endswith("/beta/sitemap.xml"))
    assert lastmods[meta_loc] == "2024-03-01"
    assert lastmods[alpha_loc] == "2024-03-01"
    assert lastmods[beta_loc] == "2024-03-01"


def test_meta_sitemap_lastmod_is_global_max(client):
    """`/meta-sitemap.xml` is a one-URL urlset listing `/` with the
    global max article date as its lastmod."""
    import xml.etree.ElementTree as ET

    _clear_sitemap_cache()
    r = client.get("/meta-sitemap.xml")
    assert r.status_code == 200
    assert r.mimetype == "application/xml"
    root = ET.fromstring(r.get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = root.findall("s:url", ns)
    assert len(urls) == 1
    assert urls[0].find("s:loc", ns).text.endswith("/")
    assert urls[0].find("s:lastmod", ns).text == "2024-03-01"


def test_inbox_sitemap_dashboard_lastmod_is_inbox_max(client):
    """First entry of `/<inbox>/sitemap.xml` is the inbox dashboard,
    with the per-inbox max as lastmod. alpha's range is 2024-01-01
    to 2024-03-01 (art3 cross-post); beta's is 2024-02-01 to
    2024-03-01."""
    import xml.etree.ElementTree as ET

    _clear_sitemap_cache()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for inbox in ("alpha", "beta"):
        root = ET.fromstring(client.get(f"/{inbox}/sitemap.xml").get_data())
        urls = root.findall("s:url", ns)
        assert urls[0].find("s:loc", ns).text.endswith(f"/{inbox}/")
        assert urls[0].find("s:lastmod", ns).text == "2024-03-01"


def test_inbox_sitemap_article_lastmod_matches_article_date(client):
    """In a per-inbox sitemap, the per-article `<lastmod>` is the
    article's own date in YYYY-MM-DD."""
    import xml.etree.ElementTree as ET
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        art1 = s.execute(
            select(Article).where(Article.message_id == "art1@example.com")
        ).scalar_one()
        art1_id = art1.id
        art1_date = art1.date

    _clear_sitemap_cache()
    r = client.get("/alpha/sitemap.xml")
    root = ET.fromstring(r.get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    art1_lastmod = None
    for u in root.findall("s:url", ns):
        loc = u.find("s:loc", ns).text
        if loc.endswith(f"/{art1_id}"):
            lm = u.find("s:lastmod", ns)
            art1_lastmod = lm.text if lm is not None else None
            break
    assert art1_lastmod == art1_date.strftime("%Y-%m-%d")


def test_inbox_sitemap_lists_year_and_month_archives(client):
    """Year + month archives that actually have data appear as
    lastmod-less `<url>` entries (discovery anchors, not refresh
    signals). Empty months are skipped, `/alpha/2024/` and
    `/alpha/2024/01/` are present, but only months with messages."""
    import xml.etree.ElementTree as ET

    _clear_sitemap_cache()
    r = client.get("/alpha/sitemap.xml")
    root = ET.fromstring(r.get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = {u.find("s:loc", ns).text for u in root.findall("s:url", ns)}
    assert any(loc.endswith("/alpha/2024/") for loc in locs)
    # alpha has messages in 2024-01 (art1, art4) and 2024-03 (art3),
    # but not 2024-02.
    assert any(loc.endswith("/alpha/2024/01/") for loc in locs)
    assert any(loc.endswith("/alpha/2024/03/") for loc in locs)
    assert not any(loc.endswith("/alpha/2024/02/") for loc in locs)


def test_inbox_sitemap_404_for_unknown_inbox(client):
    """Unknown inbox slug returns 404 rather than rendering an empty
    sitemap."""
    r = client.get("/notreal/sitemap.xml")
    assert r.status_code == 404


def test_inbox_sitemap_articles_scoped_to_that_inbox(client):
    """Articles only linked to beta (art2) don't appear in alpha's
    sitemap, and vice versa."""
    import xml.etree.ElementTree as ET
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        art2 = s.execute(
            select(Article).where(Article.message_id == "art2@example.com")
        ).scalar_one()
        art2_id = art2.id

    _clear_sitemap_cache()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    alpha_root = ET.fromstring(client.get("/alpha/sitemap.xml").get_data())
    alpha_locs = {u.find("s:loc", ns).text for u in alpha_root.findall("s:url", ns)}
    assert not any(loc.endswith(f"/{art2_id}") for loc in alpha_locs)
    beta_root = ET.fromstring(client.get("/beta/sitemap.xml").get_data())
    beta_locs = {u.find("s:loc", ns).text for u in beta_root.findall("s:url", ns)}
    assert any(loc.endswith(f"/{art2_id}") for loc in beta_locs)


# ---------------------------------------------------------------------------
# Conditional GET / Last-Modified contract (the bit Google needs to
# re-fetch the sitemap on a real change).
# ---------------------------------------------------------------------------


def test_sitemap_xml_carries_last_modified_header(client):
    """The sitemap index response sets `Last-Modified` to the global
    most-recent article date. Without this header, Google has no
    cheap way to know the sitemap changed and deprioritises
    re-fetching."""
    _clear_sitemap_cache()
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.headers.get("Last-Modified") is not None
    # Seeded data's max article date is art3's 2024-03-01. The header is
    # RFC 9110 HTTP-date format (start of day UTC), so the string must
    # at least mention that calendar date.
    assert "01 Mar 2024" in r.headers["Last-Modified"]


def test_meta_sitemap_xml_carries_last_modified_header(client):
    """`/meta-sitemap.xml`'s `Last-Modified` matches the global-max
    article date (same value its body's `<lastmod>` carries)."""
    _clear_sitemap_cache()
    r = client.get("/meta-sitemap.xml")
    assert r.status_code == 200
    assert r.headers.get("Last-Modified") is not None
    assert "01 Mar 2024" in r.headers["Last-Modified"]


def test_inbox_sitemap_xml_carries_last_modified_header(client):
    """Per-inbox sitemap's `Last-Modified` matches the inbox's
    most-recent article date. Scoped per-inbox so updates to one
    inbox don't make Google re-fetch sitemaps for unchanged
    inboxes."""
    _clear_sitemap_cache()
    r = client.get("/alpha/sitemap.xml")
    assert r.status_code == 200
    assert r.headers.get("Last-Modified") is not None
    # alpha's max article date is also 2024-03-01 (art3 cross-post).
    assert "01 Mar 2024" in r.headers["Last-Modified"]


def test_sitemap_xml_returns_304_when_if_modified_since_matches(client):
    """A crawler that already saw today's content sends
    `If-Modified-Since: <date>`; mimir returns 304 Not Modified with
    no body. This is the cheap path: Google avoids re-downloading;
    mimir avoids recomputing (the body is cached anyway but a 304
    skips even the header-write)."""
    _clear_sitemap_cache()
    # First request: prime + grab the Last-Modified value.
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    last_mod = r.headers["Last-Modified"]
    # Second request, conditional: must come back 304 with no body.
    r2 = client.get("/sitemap.xml", headers={"If-Modified-Since": last_mod})
    assert r2.status_code == 304
    # 304 responses must not carry a body per RFC 9110 section 15.4.5.
    assert r2.get_data() == b""


def test_inbox_sitemap_xml_returns_304_when_if_modified_since_matches(client):
    """Conditional GET on a per-inbox sitemap: same 304 contract."""
    _clear_sitemap_cache()
    r = client.get("/alpha/sitemap.xml")
    assert r.status_code == 200
    last_mod = r.headers["Last-Modified"]
    r2 = client.get("/alpha/sitemap.xml", headers={"If-Modified-Since": last_mod})
    assert r2.status_code == 304
    assert r2.get_data() == b""


def test_sitemap_xml_returns_200_when_if_modified_since_is_older(client):
    """When the crawler's `If-Modified-Since` is older than the
    sitemap's `Last-Modified`, mimir returns 200 with the fresh body.
    This is the path that gets Google to re-index after an ingest."""
    _clear_sitemap_cache()
    # Use a Sunday in 2020 as the conditional date: well before any
    # seeded article. mimir must return the current body.
    r = client.get(
        "/sitemap.xml",
        headers={"If-Modified-Since": "Sun, 01 Mar 2020 00:00:00 GMT"},
    )
    assert r.status_code == 200
    # Body actually carries content (not a 304 leak).
    assert b"<sitemapindex" in r.get_data()
