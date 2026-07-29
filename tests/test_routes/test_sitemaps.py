"""Tests for mimir/web/routes/sitemaps.py: the meta-sitemap,
per-inbox sitemap, maintainers sitemap, `<lastmod>` correctness,
cache invalidation after canonical-inbox flips."""

import pytest

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


def test_inbox_sitemap_article_lastmod_is_the_threads_latest_activity(client):
    """A thread entry's `<lastmod>` is the date of the NEWEST message
    in the thread, not the root's own date.

    The URL keeps the root's date (that is the thread's identity), so
    the two dates appear in the same entry and must not be conflated.
    This test previously pinned the root's date, which was the correct
    contract only while deriving last activity needed a recursive walk;
    the materialised thread root made it a grouped aggregate.

    The seeded thread has a reply strictly newer than its root, so this
    fails if the two are swapped back.
    """
    import xml.etree.ElementTree as ET
    from sqlalchemy import func, select
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    with SessionLocal() as s:
        art1 = s.execute(
            select(Article).where(Article.message_id == "art1@example.com")
        ).scalar_one()
        art1_id = art1.id
        art1_date = art1.date
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        newest = s.scalar(
            select(func.max(Article.date))
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == alpha.id,
                ArticleList.thread_root_id == art1_id,
            )
        )
    assert newest is not None and newest > art1_date, (
        "fixture no longer has a reply newer than its root, so this test "
        "cannot tell start-date from last-activity"
    )

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
    assert art1_lastmod == newest.strftime("%Y-%m-%d"), (
        f"expected the thread's latest activity {newest:%Y-%m-%d}, got "
        f"{art1_lastmod} (the root's own date is {art1_date:%Y-%m-%d})"
    )


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


def test_inbox_sitemap_root_query_uses_the_materialised_column():
    """The root test is a column comparison, not a subquery.

    This replaces a guard on the EXISTS-versus-JOIN spelling, which was
    load-bearing while the predicate had to derive rootness from
    `thread_parent`: one form was ~37x slower on the ~199 small
    inboxes, the other ~8x slower on lkml, and getting it wrong cost
    two review rounds. Materialising the root removes the choice
    entirely, so what needs pinning now is that the query stays on the
    column rather than drifting back to deriving it.
    """
    from sqlalchemy import select

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

    assert "thread_root_id = articles.id" in sql.replace("\n", " "), sql
    assert "EXISTS" not in sql.upper(), (
        f"the root test derived rootness again instead of reading the column: {sql}"
    )

    # Separate, coarser guard, carried over from the pre-column version
    # of this test: whatever the spelling, the query must never
    # degenerate into a full table scan. This catches a dropped index or
    # a predicate rewrite that defeats one, neither of which the string
    # assertions above can see. Both of this workstream's measured
    # performance disasters were plan-shape regressions.
    from sqlalchemy import text

    with SessionLocal() as s:
        plan = [row[-1] for row in s.execute(text("EXPLAIN QUERY PLAN " + sql))]
    assert not any("SCAN articles" in step for step in plan), plan


def test_sitemap_is_coherent_midway_through_a_backfill(client, tmp_path):
    """The state between `seed_roots` and the first `propagate`.

    Both backfill drivers commit one pass at a time (the RPC handler
    per WriteOp, the broker's startup path via the same `drive_passes`
    seam), so this is a real committed state on every inbox,
    and a durable one if the run is interrupted (broker restart, RPC
    timeout, pass-budget exhaustion). Nothing rendered a sitemap
    against a partially-filled corpus before, which is how a version of
    `_singleton_root_ids` that counts only POPULATED members shipped:
    a multi-message thread whose replies were still NULL counted as one
    and was published as a message URL, while its own page
    canonicalised to the thread URL.

    Whatever the sitemap lists must agree with that page's canonical,
    at every point during the backfill, not just after it.

    Uses a real ingested corpus rather than the shared fixture, whose
    rows carry synthetic commit_shas and so 404 on the message page.
    """
    import re
    import xml.etree.ElementTree as ET

    from sqlalchemy import select, update

    from mimir import cache
    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList, Inbox
    from mimir.thread_roots import seed_roots
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [
            ("mb-root@x", None),
            ("mb-reply@x", "mb-root@x"),
            ("mb-nested@x", "mb-reply@x"),
            ("mb-solo@x", None),
        ],
    )
    ours = {url for _aid, url in seeded.values()}

    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        # Rewind to the post-migration state, then run ONLY the seed
        # pass: roots populated, replies still NULL.
        s.execute(
            update(ArticleList)
            .where(ArticleList.inbox_id == alpha.id)
            .values(thread_root_id=None)
        )
        s.commit()
        seed_roots(s, alpha.id)
        s.commit()

    cache.delete_for_inbox("alpha")
    cache.delete("sitemap:index")

    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(client.get("/alpha/sitemap.xml").get_data())
    locs = [u.find("s:loc", ns).text.replace("http://localhost", "") for u in root]

    checked = 0
    for loc in locs:
        page = loc[:-2] if loc.endswith("/t") else loc
        if page not in ours:
            continue  # conftest rows have synthetic blobs and 404
        html = client.get(page).get_data(as_text=True)
        m = re.search(r'<link rel="canonical" href="([^"]+)"', html)
        assert m, f"{page} emitted no canonical"
        canonical = m.group(1).replace("http://localhost", "")
        assert loc == canonical, (
            f"mid-backfill the sitemap lists {loc} but that page's "
            f"canonical is {canonical}"
        )
        checked += 1

    assert checked, "no ingested thread reached the sitemap; test proves nothing"


def _active_thread(article_id, *, reply_count, subject="A thread"):
    from datetime import datetime, timezone

    from mimir.threading import ActiveThread

    return ActiveThread(
        id=article_id,
        inbox_name="alpha",
        message_id=f"t{article_id}@x",
        subject=subject,
        author="a@b.example",
        date=datetime(2024, 3, 1, tzinfo=timezone.utc),
        recent_count=reply_count + 1,
        reply_count=reply_count,
        last_activity=datetime(2024, 3, 1, tzinfo=timezone.utc),
    )


def test_inbox_json_ld_item_list_points_at_thread_views(client):
    """The `ItemList` on `/<inbox>/` claims to be the active
    DISCUSSIONS, so a discussion with replies must point at the thread
    view rather than at its root message.

    Pointing at the root message put structured data handed to Google
    at odds with the page it names, which disclaims itself via
    `<link rel="canonical">`.
    """
    from mimir.seo.json_ld import _json_ld_inbox

    class _Inbox:
        name = "alpha"

    payload = _json_ld_inbox(
        "https://example.test",
        _Inbox(),
        active_threads=[
            _active_thread(11, reply_count=4, subject="Has replies"),
            _active_thread(22, reply_count=0, subject="Solo"),
        ],
    )
    urls = [item["url"] for item in payload["mainEntity"]["itemListElement"]]
    assert urls[0] == "https://example.test/alpha/2024/03/11/t", urls
    # A single-message thread keeps its own URL: its message page IS
    # the whole conversation and is the richer of the two.
    assert urls[1] == "https://example.test/alpha/2024/03/22", urls


def _set_article_date(article_id: int, when):
    """Force one article's `date`.

    `articles.date` is the public-inbox commit timestamp, not the
    RFC 5322 `Date:` header, so the seeding helper's `date_for` (which
    writes the header) cannot move it.
    """
    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        s.execute(update(Article).where(Article.id == article_id).values(date=when))
        s.commit()


def test_thread_lastmod_is_scoped_to_the_inbox(client, tmp_path):
    """The same root can head threads of different lengths in each
    inbox it is cross-posted to, so its last-activity date differs per
    inbox.

    Written first because this is the axis that produced three separate
    blocking bugs in the surrounding work: every test held it at one
    inbox, so dropping the inbox scoping entirely passed the suite.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from mimir.seo.sitemaps import _thread_last_activity
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("la1@x", None), ("la2@x", "la1@x")])
    root_id = seeded["la1@x"][0]
    # `articles.date` is the public-inbox COMMIT time, not the `Date:`
    # header (CONTEXT.md), so `seed_thread_shape`'s `date_for` cannot
    # move it. Set it directly.
    _set_article_date(seeded["la2@x"][0], datetime(2024, 6, 5, tzinfo=timezone.utc))

    # Cross-post the ROOT only into beta: alpha's thread runs to June,
    # beta's copy is a lone January message.
    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        src = (
            s.execute(select(ArticleList).where(ArticleList.article_id == root_id))
            .scalars()
            .first()
        )
        s.add(
            ArticleList(
                article_id=root_id,
                inbox_id=beta.id,
                epoch=src.epoch,
                commit_sha=src.commit_sha,
                thread_root_id=root_id,
            )
        )
        s.commit()

        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        alpha_latest = _thread_last_activity(s, alpha, [root_id])[root_id]
        beta_latest = _thread_last_activity(s, beta, [root_id])[root_id]
        root_date = s.get(Article, root_id).date

    assert alpha_latest.strftime("%Y-%m") == "2024-06", alpha_latest
    assert beta_latest == root_date, (
        f"beta holds only the root, so its thread's last activity is the "
        f"root's own date; got {beta_latest}"
    )


def test_thread_lastmod_understates_rather_than_overstates_mid_backfill(
    client, tmp_path
):
    """A reply whose `thread_root_id` is not yet filled is not counted.

    Be precise about what this does and does not guard, because the
    name oversells it. It catches ZERO single-line mutations of the
    query: `IN (...)` excludes NULL and `GROUP BY` buckets NULL
    separately, so the assertion is a SQL tautology for any variation
    of this shape. Do not trust it to pin the mid-backfill arithmetic.

    What it does pin, and is the only test in the suite that does, is
    the MEMBERSHIP SOURCE: rewrite membership from the materialised
    column to a `thread_parent` walk and this is what fails, because
    such a walk would count the unbackfilled reply. That matters
    because the sibling `_singleton_root_ids` deliberately uses the
    `thread_parent` form, so "make these two consistent" is a plausible
    future edit, and it would silently change what a mid-backfill
    sitemap advertises.

    The safety direction it documents is real: `<lastmod>` can only
    DELAY a re-crawl, never pin one, so understating costs freshness
    until the backfill lands, while overstating would spend crawl
    budget on documents that had not changed.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select, update

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from mimir.seo.sitemaps import _thread_last_activity
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("mb1@x", None), ("mb2@x", "mb1@x")])
    root_id = seeded["mb1@x"][0]
    reply_id = seeded["mb2@x"][0]
    _set_article_date(reply_id, datetime(2024, 6, 5, tzinfo=timezone.utc))

    with SessionLocal() as s:
        s.execute(
            update(ArticleList)
            .where(ArticleList.article_id == reply_id)
            .values(thread_root_id=None)
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        latest = _thread_last_activity(s, alpha, [root_id])[root_id]
        root_date = s.get(Article, root_id).date

    assert latest == root_date, (
        f"an unbackfilled reply must not contribute; got {latest}"
    )


@pytest.mark.parametrize(
    "shape,newest",
    [
        # (message_id -> day-of-month) plus which one is newest. The
        # point of every case is that the newest message is NOT the
        # last one to arrive, so "last row wins" and "max" disagree.
        ({"n1": 9, "n2": 2, "n3": 3}, "n1"),  # newest is the root
        ({"n1": 1, "n2": 9, "n3": 3}, "n2"),  # newest in the middle
        ({"n1": 1, "n2": 3, "n3": 2}, "n2"),  # newest is an earlier sibling
    ],
    ids=["newest_is_root", "newest_in_middle", "newest_earlier_sibling"],
)
def test_thread_lastmod_is_the_max_not_the_last_row(client, tmp_path, shape, newest):
    """The aggregate must be a MAX, not "whichever member came last".

    `group_by(thread_root_id)` -> `group_by(article_id)` survived the
    ENTIRE suite, because every existing fixture happens to have its
    newest-dated member arrive last, which makes last-row-wins and max
    indistinguishable. The axis held fixed was WHICH MEMBER CARRIES THE
    NEWEST DATE.

    That axis is not exotic here: `articles.date` is the archive commit
    time, so a child legitimately predating its parent is documented
    behaviour, not a corner case.

    The expected value is computed from the dates this test sets, not
    by re-running the production query, so it cannot agree with a
    broken implementation by construction.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from mimir.seo.sitemaps import _thread_last_activity
    from tests.test_routes._helpers import seed_thread_shape

    mids = list(shape)
    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [(f"{mids[0]}@x", None)] + [(f"{m}@x", f"{mids[0]}@x") for m in mids[1:]],
    )
    for mid, day in shape.items():
        _set_article_date(
            seeded[f"{mid}@x"][0], datetime(2024, 5, day, tzinfo=timezone.utc)
        )

    root_id = seeded[f"{mids[0]}@x"][0]
    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        got = _thread_last_activity(s, alpha, [root_id])[root_id]

    # Compare the rendered form: that is what reaches the sitemap, and
    # SQLite hands back naive datetimes where the seeder wrote aware ones.
    assert got.strftime("%Y-%m-%d") == f"2024-05-{shape[newest]:02d}", (
        f"expected the max ({newest}, day {shape[newest]}), got {got}"
    )


def test_singleton_lastmod_survives_a_corrupt_thread_root(client, tmp_path):
    """A single-message page never changes, so its `<lastmod>` must be
    its own date even if the materialised column says otherwise.

    `_singleton_root_ids` reads `thread_parent` while
    `_thread_last_activity` reads `article_lists.thread_root_id`. When
    those disagree, which only a wrong column value can cause, the
    aggregate can hand a singleton a date from some other thread. This
    diff is what turns such a column bug from "the wrong URL is listed"
    into "a page that cannot have changed claims it did", the direction
    the design calls unsafe, so the coherence is asserted rather than
    assumed.

    Verified by pointing an unrelated article's root at this one, which
    is what a W8 write-path bug produces.
    """
    import xml.etree.ElementTree as ET
    from datetime import datetime, timezone

    from sqlalchemy import select, update

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from tests.test_routes._helpers import seed_thread_shape

    solo = seed_thread_shape(tmp_path, "alpha", [("cs1@x", None)])
    other_mirror = tmp_path / "other"
    other_mirror.mkdir()
    other = seed_thread_shape(other_mirror, "alpha", [("cs2@x", None)])
    solo_id = solo["cs1@x"][0]
    _set_article_date(other["cs2@x"][0], datetime(2029, 12, 31, tzinfo=timezone.utc))

    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        # The corruption: an unrelated article claims this root.
        s.execute(
            update(ArticleList)
            .where(
                ArticleList.article_id == other["cs2@x"][0],
                ArticleList.inbox_id == alpha.id,
            )
            .values(thread_root_id=solo_id)
        )
        s.commit()
        solo_date = s.get(Article, solo_id).date

    _clear_sitemap_cache()
    root = ET.fromstring(client.get("/alpha/sitemap.xml").get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for u in root.findall("s:url", ns):
        loc = u.find("s:loc", ns).text
        if loc.endswith(f"/{solo_id}"):
            lm = u.find("s:lastmod", ns)
            assert lm is not None and lm.text == solo_date.strftime("%Y-%m-%d"), (
                f"single-message page {loc} advertises lastmod {lm.text if lm is not None else None}, "
                f"but it can only ever have changed on {solo_date:%Y-%m-%d}"
            )
            break
    else:
        raise AssertionError(f"singleton {solo_id} absent from the sitemap")


def _locs(client, url):
    import xml.etree.ElementTree as ET

    r = client.get(url)
    assert r.status_code == 200, f"{url} -> {r.status_code}"
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(r.get_data())
    return [u.find("s:loc", ns).text for u in root.findall("s:url", ns)]


def test_month_sitemap_reaches_threads_the_flat_sitemap_caps_off(
    client, tmp_path, monkeypatch
):
    """The point of the whole workstream: the deep archive.

    `/<inbox>/sitemap.xml` lists only the most recent
    `SITEMAP_RECENT_PER_INBOX` threads, so on the real corpus
    (28.8M rows) everything older sat in NO sitemap and was
    unreachable by sitemap-driven discovery. A thread past that cap
    must still appear in its month.
    """
    from datetime import datetime, timezone

    import mimir.seo.sitemaps as sm
    from tests.test_routes._helpers import seed_thread_shape

    monkeypatch.setattr(sm, "SITEMAP_RECENT_PER_INBOX", 1)
    seeded = seed_thread_shape(tmp_path, "alpha", [("old1@x", None)])
    old_id = seeded["old1@x"][0]
    _set_article_date(old_id, datetime(2011, 3, 9, tzinfo=timezone.utc))

    other = tmp_path / "recent"
    other.mkdir()
    newer = seed_thread_shape(other, "alpha", [("new1@x", None)])
    _set_article_date(newer["new1@x"][0], datetime(2026, 7, 1, tzinfo=timezone.utc))

    _clear_sitemap_cache()
    flat = _locs(client, "/alpha/sitemap.xml")
    assert not any(f"/{old_id}" in loc for loc in flat), (
        "fixture failed to push the old thread past the recent cap"
    )
    month = _locs(client, "/alpha/2011/03/sitemap.xml")
    assert any(loc.endswith(f"/{old_id}") for loc in month), month


def test_month_sitemap_pages_are_disjoint_and_complete(client, tmp_path, monkeypatch):
    """Pages are `LIMIT`/`OFFSET` slices, so an unstable or
    non-deterministic order silently drops and duplicates URLs across
    the boundary. Assert the union is exactly the whole month and the
    intersection is empty.

    Page size is monkeypatched rather than seeding 45,000 threads; the
    boundary behaviour is what matters and it is size-independent.
    """
    from datetime import datetime, timezone

    import mimir.seo.sitemaps as sm
    from tests.test_routes._helpers import seed_thread_shape

    monkeypatch.setattr(sm, "SITEMAP_URLS_PER_PAGE", 2)
    ids = []
    for i in range(5):
        mirror = tmp_path / f"m{i}"
        mirror.mkdir()
        seeded = seed_thread_shape(mirror, "alpha", [(f"pg{i}@x", None)])
        art_id = seeded[f"pg{i}@x"][0]
        _set_article_date(art_id, datetime(2019, 4, 1 + i, tzinfo=timezone.utc))
        ids.append(art_id)

    _clear_sitemap_cache()
    p1 = _locs(client, "/alpha/2019/04/sitemap.xml")
    p2 = _locs(client, "/alpha/2019/04/sitemap-2.xml")
    p3 = _locs(client, "/alpha/2019/04/sitemap-3.xml")

    assert len(p1) == 2 and len(p2) == 2 and len(p3) == 1, (p1, p2, p3)
    seen = p1 + p2 + p3
    assert len(seen) == len(set(seen)), f"a URL appears on two pages: {seen}"
    assert {loc.rsplit("/", 1)[-1] for loc in seen} == {str(i) for i in ids}, seen

    # One past the end is absent, not an empty urlset a crawler would
    # keep re-fetching.
    assert client.get("/alpha/2019/04/sitemap-4.xml").status_code == 404


def test_sitemap_index_enumerates_every_month_page(client, tmp_path, monkeypatch):
    """sitemaps.org forbids an index referencing another index, so the
    pages cannot hide behind a nested index and the top-level index
    must name each one. An index that advertises fewer pages than exist
    leaves those URLs undiscoverable, which is the failure this
    workstream is about.
    """
    import xml.etree.ElementTree as ET
    from datetime import datetime, timezone

    import mimir.seo.sitemaps as sm
    from tests.test_routes._helpers import seed_thread_shape

    monkeypatch.setattr(sm, "SITEMAP_URLS_PER_PAGE", 2)
    for i in range(3):
        mirror = tmp_path / f"ix{i}"
        mirror.mkdir()
        seeded = seed_thread_shape(mirror, "alpha", [(f"ixp{i}@x", None)])
        _set_article_date(
            seeded[f"ixp{i}@x"][0], datetime(2015, 8, 1 + i, tzinfo=timezone.utc)
        )

    _clear_sitemap_cache()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(client.get("/sitemap.xml").get_data())
    locs = {sm_el.find("s:loc", ns).text for sm_el in root.findall("s:sitemap", ns)}
    aug = {loc for loc in locs if "/alpha/2015/08/" in loc}
    assert len(aug) == 2, f"expected 2 pages for 3 roots at size 2, got {aug}"
    # Everything the index advertises must resolve.
    for loc in aug:
        assert client.get(loc[loc.index("/alpha") :]).status_code == 200, loc


def test_month_sitemap_rejects_impossible_months(client):
    """Both segments come from the URL, so an out-of-range month would
    otherwise build a nonsense date range rather than 404."""
    for bad in ("/alpha/2024/00/sitemap.xml", "/alpha/2024/13/sitemap.xml"):
        assert client.get(bad).status_code == 404, bad


def test_month_sitemap_is_scoped_to_its_inbox(client, tmp_path):
    """The axis that produced three blocking bugs in this workstream:
    every test used one inbox, so dropping the inbox filter passed."""
    from datetime import datetime, timezone

    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("sc1@x", None)])
    art_id = seeded["sc1@x"][0]
    _set_article_date(art_id, datetime(2013, 2, 4, tzinfo=timezone.utc))

    _clear_sitemap_cache()
    assert any(
        loc.endswith(f"/{art_id}")
        for loc in _locs(client, "/alpha/2013/02/sitemap.xml")
    )
    assert client.get("/beta/2013/02/sitemap.xml").status_code == 404, (
        "alpha's thread leaked into beta's month sitemap"
    )


def test_month_sitemap_pages_stay_disjoint_when_dates_collide(
    client, tmp_path, monkeypatch
):
    """Ties in the sort key are the case that actually needs the
    tiebreak.

    `LIMIT`/`OFFSET` over an ORDER BY with ties has no defined order in
    SQL, so page 1 and page 2 may both return the same row and drop
    another. Real commit timestamps collide constantly inside a busy
    month, so this is the normal case, not a corner one, and the
    sibling paging test cannot see it because every root there has a
    distinct date.

    Be honest about the limit of this guard: removing the `Article.id`
    tiebreak does NOT make it fail. SQLite happens to order this plan
    stably, so no mutation available here falsifies the tiebreak. It is
    kept as defence against a plan change (a different index chosen
    after ANALYZE, a schema change) making the order non-deterministic,
    where the failure would be silently dropped URLs. What this test
    genuinely pins is the disjoint-and-complete property of paging,
    with ties present.
    """
    from datetime import datetime, timezone

    import mimir.seo.sitemaps as sm
    from tests.test_routes._helpers import seed_thread_shape

    monkeypatch.setattr(sm, "SITEMAP_URLS_PER_PAGE", 2)
    same = datetime(2018, 9, 12, 6, 30, 0, tzinfo=timezone.utc)
    ids = []
    for i in range(5):
        mirror = tmp_path / f"tie{i}"
        mirror.mkdir()
        seeded = seed_thread_shape(mirror, "alpha", [(f"tie{i}@x", None)])
        art_id = seeded[f"tie{i}@x"][0]
        _set_article_date(art_id, same)
        ids.append(art_id)

    _clear_sitemap_cache()
    seen = (
        _locs(client, "/alpha/2018/09/sitemap.xml")
        + _locs(client, "/alpha/2018/09/sitemap-2.xml")
        + _locs(client, "/alpha/2018/09/sitemap-3.xml")
    )
    assert len(seen) == len(set(seen)), f"a URL appears on two pages: {seen}"
    assert {loc.rsplit("/", 1)[-1] for loc in seen} == {str(i) for i in ids}, seen


def test_month_sitemap_lists_roots_not_replies(client, tmp_path):
    """One URL per conversation, exactly as the flat sitemap does.

    Every other month-sitemap test seeds SINGLE-MESSAGE threads, so
    "roots, not replies" was never exercised and three separate
    mutations survived the whole suite: dropping the root predicate
    made the month sitemap emit every message with `/t` glued onto
    replies, i.e. non-canonical URLs that each page disclaims.

    Asserted against the flat sitemap rather than a hardcoded list, so
    the two can never drift apart on the same thread.
    """
    from datetime import datetime, timezone

    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [("rr1@x", None), ("rr2@x", "rr1@x"), ("rr3@x", "rr2@x")],
    )
    when = datetime(2023, 9, 14, tzinfo=timezone.utc)
    for mid in seeded:
        _set_article_date(seeded[mid][0], when)
    root_id = seeded["rr1@x"][0]

    _clear_sitemap_cache()
    month = _locs(client, "/alpha/2023/09/sitemap.xml")
    assert month == [
        loc for loc in _locs(client, "/alpha/sitemap.xml") if f"/{root_id}" in loc
    ], month
    assert len(month) == 1 and month[0].endswith(f"/{root_id}/t"), month
    for mid in ("rr2@x", "rr3@x"):
        assert not any(str(seeded[mid][0]) in loc for loc in month), (
            f"{mid} is a reply and must not get its own sitemap URL: {month}"
        )


def test_month_sitemap_excludes_the_first_instant_of_the_next_month(client, tmp_path):
    """The range is half-open. `<=` instead of `<` admits a root dated
    exactly `YYYY-MM-01 00:00:00` into the previous month, which both
    duplicates it and desyncs the page from the index's own count
    (that buckets via strftime). Commit timestamps have second
    precision, so this is reachable, not theoretical.
    """
    from datetime import datetime, timezone

    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("bd1@x", None)])
    edge_id = seeded["bd1@x"][0]
    _set_article_date(edge_id, datetime(2020, 7, 1, 0, 0, 0, tzinfo=timezone.utc))

    _clear_sitemap_cache()
    assert client.get("/alpha/2020/06/sitemap.xml").status_code == 404, (
        "a root dated midnight on 1 July leaked into June"
    )
    assert any(
        loc.endswith(f"/{edge_id}")
        for loc in _locs(client, "/alpha/2020/07/sitemap.xml")
    )


def test_month_sitemap_404s_do_not_write_cache_rows(client, tmp_path):
    """A 404 must not cost a write.

    `year`, `month` and `page` come straight off the URL, so the key
    space is unbounded and unauthenticated. Caching misses turns free
    404s into a stream of writes through the broker, which is the
    single SQLite writer for the whole system and already the
    contended resource. Review confirmed 78 requests producing 78
    cache rows.
    """
    from sqlalchemy import func, select

    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    def rows() -> int:
        with SessionLocal() as s:
            return s.scalar(select(func.count()).select_from(CacheEntry))

    _clear_sitemap_cache()
    before = rows()
    for url in (
        "/alpha/2031/01/sitemap.xml",
        "/alpha/2031/02/sitemap.xml",
        "/alpha/2031/03/sitemap-2.xml",
        "/alpha/1999/11/sitemap-7.xml",
    ):
        assert client.get(url).status_code == 404, url
    assert rows() == before, "a 404 wrote a cache row"


def test_month_sitemap_rejects_out_of_range_segments_without_500ing(client):
    """Year 0 and year 9999 both raise from `datetime` (the latter
    because the month range needs `year + 1`), and a huge page number
    used to overflow on the way into SQLite. All were 500s on public
    URLs, reachable with no data and no auth."""
    for bad in (
        "/alpha/2024/00/sitemap.xml",
        "/alpha/2024/13/sitemap.xml",
        "/alpha/0/1/sitemap.xml",
        "/alpha/9999/12/sitemap.xml",
        "/alpha/99999/1/sitemap.xml",
        "/alpha/2024/06/sitemap-1000000000000000.xml",
    ):
        assert client.get(bad).status_code == 404, f"{bad} did not 404"


def test_month_sitemap_sends_cache_control(client, tmp_path):
    """Every other sitemap surface is edge-shielded; these are ~32k
    URLs the index actively points crawlers at, and they are not
    warm-cache targets, so each miss is a cold compute at the origin."""
    from datetime import datetime, timezone

    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("cc1@x", None)])
    _set_article_date(seeded["cc1@x"][0], datetime(2017, 5, 3, tzinfo=timezone.utc))
    _clear_sitemap_cache()
    r = client.get("/alpha/2017/05/sitemap.xml")
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "public, max-age=300", dict(r.headers)


def test_inbox_sitemap_lists_the_subsystem_index_and_active_subsystems(
    client, tmp_path
):
    """Subsystem dashboards are distinctive hub pages nothing else
    mirrors, and were in no sitemap at all.

    The ACTIVE set only. The full MAINTAINERS taxonomy is ~3,300
    sections and the route is per-inbox, so advertising all of them
    from all ~200 production inboxes would be ~660,000 URLs, nearly
    all of them a page for a subsystem no patch in that inbox has
    touched. Measured 2026-07-29, the active set is 3,394 pairs.
    """
    from sqlalchemy import select as sa_select

    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from mimir.subsystems_dashboard import most_active_subsystems_in_inbox
    from tests.test_routes._helpers import _ingest_one_article, _seed_subsystem

    _seed_subsystem("BCACHEFS", "Maintained", files=["fs/bcachefs/"])
    _ingest_one_article(
        tmp_path,
        "alpha",
        "sm-patch@example.com",
        body=b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n",
    )
    with SessionLocal() as s:
        inbox = s.execute(sa_select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        most_active_subsystems_in_inbox(s, inbox, days=7)

    xml = client.get("/alpha/sitemap.xml").get_data(as_text=True)
    assert "<loc>http://localhost/alpha/subsystem/</loc>" in xml
    assert "<loc>http://localhost/alpha/subsystem/bcachefs/</loc>" in xml


def test_inbox_sitemap_omits_subsystems_rather_than_computing_on_a_cold_cache(
    client, tmp_path
):
    """The builder runs on the request path when the sitemap cache is
    cold, and the subsystem aggregation is multi-second per inbox: the
    2.8.0 regression family. It must degrade to omitting the entries,
    never to a minutes-long render.

    Warm ordering makes the miss rare rather than routine, since
    `most_active_subsystems_in_inbox` is warmed immediately before
    `sitemap:inbox:<name>` in the same slow-tier per-inbox list.
    """
    from tests.test_routes._helpers import _ingest_one_article, _seed_subsystem

    _seed_subsystem("BCACHEFS", "Maintained", files=["fs/bcachefs/"])
    _ingest_one_article(
        tmp_path,
        "alpha",
        "cold-patch@example.com",
        body=b"diff --git a/fs/bcachefs/super.c b/fs/bcachefs/super.c\n@@ -1 +1 @@\n-x\n+y\n",
    )
    # No warm step: the subsystem-activity cache is cold.
    xml = client.get("/alpha/sitemap.xml").get_data(as_text=True)
    assert "<loc>http://localhost/alpha/subsystem/</loc>" in xml, (
        "the index page is a static URL and should be listed regardless"
    )
    assert "/alpha/subsystem/bcachefs/" not in xml
