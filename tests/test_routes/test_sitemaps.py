"""Tests for mimir/web/routes/sitemaps.py: the meta-sitemap,
per-inbox sitemap, maintainers sitemap, `<lastmod>` correctness,
cache invalidation after canonical-inbox flips."""

from tests.test_routes._helpers import _clear_sitemap_cache, _seed_subsystem


def test_sitemap_xml(client):
    """`/sitemap.xml` is the sitemap index: a `<sitemapindex>` of
    `<sitemap>` children, one per inbox sub-sitemap plus
    `/meta-sitemap.xml` and `/sitemap-maintainers.xml`. Every linked
    sub-sitemap URL must itself resolve (200) -- a crawler that
    follows a 404 sitemap link reports the site as broken even if
    `/sitemap.xml` itself rendered fine."""
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
        "/sitemap-maintainers.xml",
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
    article anymore (that was the old global-sitemap design).

    art3 has no replies, so it is listed as a message URL rather than
    `/t`, matching what its own canonical says."""
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
    beta, the meta-sitemap, and the maintainers sitemap all carry
    the global max 2024-03-01 (art3's cross-post date); the
    maintainers entry reuses `global_latest` as a free proxy since
    there's no per-maintainer date to derive it from."""
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
    maintainers_loc = next(
        loc for loc in lastmods if loc.endswith("/sitemap-maintainers.xml")
    )
    assert lastmods[meta_loc] == "2024-03-01"
    assert lastmods[alpha_loc] == "2024-03-01"
    assert lastmods[beta_loc] == "2024-03-01"
    assert lastmods[maintainers_loc] == "2024-03-01"


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


def test_maintainers_sitemap_xml_lists_seeded_maintainers(client):
    """`/sitemap-maintainers.xml` is a urlset whose `<loc>`s include
    `/maintainers/<address>` for every distinct `M:` maintainer,
    address lowercased per `all_maintainers`."""
    import xml.etree.ElementTree as ET
    from urllib.parse import urlparse

    _seed_subsystem(
        "NETWORKING",
        "Maintained",
        ["net/"],
        maintainers=[("M", "Alice Maintainer", "Alice@Example.com")],
    )
    _seed_subsystem(
        "STORAGE",
        "Maintained",
        ["fs/"],
        maintainers=[("M", "Bob Maintainer", "bob@example.com")],
    )
    _clear_sitemap_cache()
    r = client.get("/sitemap-maintainers.xml")
    assert r.status_code == 200
    assert r.mimetype == "application/xml"
    root = ET.fromstring(r.get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = {u.find("s:loc", ns).text for u in root.findall("s:url", ns)}
    found_paths = {urlparse(loc).path for loc in locs}
    # all_maintainers lowercases addresses via SQL func.lower, so the
    # URL carries the lowercased form regardless of the source casing.
    assert "/maintainers/alice@example.com" in found_paths
    assert "/maintainers/bob@example.com" in found_paths


def test_maintainers_sitemap_xml_has_no_lastmod(client):
    """Slice-1 contract: no per-url `<lastmod>` in the body, and no
    `Last-Modified` response header (sitemaps are unconditional; no
    surface sets the header, see `_sitemap_response`)."""
    import xml.etree.ElementTree as ET

    _seed_subsystem(
        "NETWORKING",
        "Maintained",
        ["net/"],
        maintainers=[("M", "Alice Maintainer", "alice@example.com")],
    )
    _clear_sitemap_cache()
    r = client.get("/sitemap-maintainers.xml")
    assert r.status_code == 200
    assert r.headers.get("Last-Modified") is None
    # Now that no sitemap surface carries a conditional validator, an
    # explicit Cache-Control is the only thing keeping this one off CDN
    # heuristic caching. Guards the `web.maintainers_sitemap` entry in
    # hooks.py against being dropped.
    assert r.headers.get("Cache-Control") == "public, max-age=300"
    root = ET.fromstring(r.get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for u in root.findall("s:url", ns):
        assert u.find("s:lastmod", ns) is None


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
    """In a per-inbox sitemap, a thread entry's `<lastmod>` is the
    root article's own date in YYYY-MM-DD (the thread's start; see
    `inbox_sitemap_xml` for why not its latest activity)."""
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
        if loc.endswith(f"/{art1_id}/t"):
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
    """Threads only linked to beta (art2) don't appear in alpha's
    sitemap, and vice versa.

    art2 has no replies, so it is a single-message thread and the
    sitemap lists its MESSAGE URL (which is its canonical), not `/t`."""
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
# Unconditional-response contract. Sitemaps carry NO `Last-Modified` /
# `ETag` and never 304. The earlier date-based conditional GET pinned
# stale STRUCTURAL versions in downstream caches (Cloudflare's edge,
# crawlers): the `Last-Modified` was the max article date, which does
# not advance when a deploy changes the sitemap's shape (e.g. adding
# `/sitemap-maintainers.xml` in 3.6.0), so `If-Modified-Since` kept
# returning 304 and every edge served the pre-deploy body until the
# next day's first article bumped the date. See sitemaps.py module
# docstring.
# ---------------------------------------------------------------------------


def test_sitemap_xml_has_no_conditional_validator(client):
    """The sitemap index carries no `Last-Modified` / `ETag`, so no
    downstream cache can revalidate-and-pin a stale body."""
    _clear_sitemap_cache()
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.headers.get("Last-Modified") is None
    assert r.headers.get("ETag") is None


def test_meta_sitemap_xml_has_no_conditional_validator(client):
    """`/meta-sitemap.xml` carries no conditional-GET validator."""
    _clear_sitemap_cache()
    r = client.get("/meta-sitemap.xml")
    assert r.status_code == 200
    assert r.headers.get("Last-Modified") is None
    assert r.headers.get("ETag") is None


def test_inbox_sitemap_xml_has_no_conditional_validator(client):
    """Per-inbox sitemap carries no conditional-GET validator."""
    _clear_sitemap_cache()
    r = client.get("/alpha/sitemap.xml")
    assert r.status_code == 200
    assert r.headers.get("Last-Modified") is None
    assert r.headers.get("ETag") is None


def test_sitemap_xml_ignores_if_modified_since_returns_full_body(client):
    """Regression guard for the 3.6.0 stale-sitemap bug: a conditional
    `If-Modified-Since` (even one dated far in the future) must return
    200 with the full body, NEVER a 304. A 304 here is exactly what let
    Cloudflare pin the pre-deploy index that omitted the maintainers
    urlset."""
    _clear_sitemap_cache()
    r = client.get(
        "/sitemap.xml",
        headers={"If-Modified-Since": "Wed, 01 Jan 2031 00:00:00 GMT"},
    )
    assert r.status_code == 200
    assert b"<sitemapindex" in r.get_data()


def test_inbox_sitemap_xml_ignores_if_modified_since_returns_full_body(client):
    """Same regression guard on a per-inbox sitemap: conditional GET
    still yields 200 + body, never a stale-pinning 304."""
    _clear_sitemap_cache()
    r = client.get(
        "/alpha/sitemap.xml",
        headers={"If-Modified-Since": "Wed, 01 Jan 2031 00:00:00 GMT"},
    )
    assert r.status_code == 200
    assert b"<urlset" in r.get_data()


def test_inbox_sitemap_lists_thread_roots_not_replies(client):
    """The sitemap lists one thread URL per conversation, not one URL
    per message. art4 is a reply to art1, so only art1's thread view is
    listed; art4 has no entry of its own. Listing replies would hand
    crawlers ~10x the URLs, every one of which canonicalises to the
    thread view anyway."""
    import xml.etree.ElementTree as ET

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        art1_id = s.execute(
            select(Article.id).where(Article.message_id == "art1@example.com")
        ).scalar_one()
        art4_id = s.execute(
            select(Article.id).where(Article.message_id == "art4@example.com")
        ).scalar_one()

    _clear_sitemap_cache()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(client.get("/alpha/sitemap.xml").get_data())
    locs = {u.find("s:loc", ns).text for u in root.findall("s:url", ns)}

    assert any(loc.endswith(f"/{art1_id}/t") for loc in locs)
    # The reply appears nowhere, neither as a thread URL nor a bare
    # message URL.
    assert not any(f"/{art4_id}" in loc for loc in locs)


def test_inbox_sitemap_root_query_scopes_inbox_by_join_not_exists():
    """Guard the measured shape of the thread-root query.

    Inbox membership must stay a JOIN; see `_recent_thread_roots_query`
    for the measurements behind that (they live in one place so W8
    re-measuring them does not leave a stale copy here).

    Asserts the SQL shape rather than EXPLAIN output. The two
    formulations only diverge in the planner at production scale and
    distribution; against the test fixture SQLite resolves both the
    same way, so a plan assertion here would pass in either
    configuration and guard nothing.
    """
    from sqlalchemy import select, text

    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from mimir.seo.sitemaps import (
        SITEMAP_RECENT_PER_INBOX,
        _recent_thread_roots_query,
    )

    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        stmt = _recent_thread_roots_query(inbox).limit(SITEMAP_RECENT_PER_INBOX)
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))

    outer = sql.split("WHERE", 1)[0]
    assert "JOIN article_lists" in outer, (
        f"inbox scope must stay a join, not a correlated EXISTS: {outer!r}"
    )
    # The root test itself stays an EXISTS (cheap per-candidate probe).
    assert "EXISTS" in sql.upper()
    # Separate, coarser guard: whatever the shape, the query must never
    # degenerate into a full table scan. Catches a dropped index or a
    # predicate rewrite that defeats one, which the shape assertion
    # above cannot see.
    with SessionLocal() as s:
        plan = [row[-1] for row in s.execute(text("EXPLAIN QUERY PLAN " + sql))]
    assert not any("SCAN articles" in step for step in plan), plan
