"""`/sitemap.xml`, `/meta-sitemap.xml`, `/sitemap-maintainers.xml`,
and `/<inbox>/sitemap.xml`.

`/sitemap.xml` is a `<sitemapindex>` pointing at `/meta-sitemap.xml`
(a one-URL urlset for `/`), one `/<inbox>/sitemap.xml` per configured
inbox, and `/sitemap-maintainers.xml` (a urlset of every maintainer
profile page). Crawlers fetch the sub-sitemaps independently and
skip unchanged inboxes via each entry's `<lastmod>`. The index/meta
split (issue #10) replaced the earlier single monolithic urlset;
cross-posted articles now appear in each linked inbox's sitemap
(every one is a real URL, and the page's own `<link rel="canonical">`
tells search engines which to keep).

Responses are deliberately unconditional: they carry no
`Last-Modified` / `ETag` validator and always return 200 with the
current body. An earlier version set `Last-Modified` to the sitemap's
newest content date and honoured `If-Modified-Since` (304 Not
Modified), but that date only advances when a newer article lands,
not when the sitemap's *structure* changes via a deploy (e.g. adding
the `/sitemap-maintainers.xml` entry in 3.6.0). Conditional GET then
pinned every downstream cache (Cloudflare's edge, and any crawler
issuing `If-Modified-Since`) to the pre-deploy body until the next
day's first article happened to bump the date past the cached value.
Dropping the HTTP validator makes each fetch a full 200; the per-URL
`<lastmod>` inside the XML stays the crawler-facing freshness signal,
and the per-endpoint `Cache-Control: max-age=300` (set in hooks.py)
still lets edges cache briefly and re-fetch fresh.
"""

from flask import Response

from mimir.extensions import SessionLocal
from mimir.seo import (
    inbox_sitemap_xml,
    maintainers_sitemap_xml,
    meta_sitemap_xml,
    sitemap_index_xml,
)
from mimir.seo.sitemaps import SitemapPayload
from mimir.web._blueprint import bp_web
from mimir.web.urls import _get_inbox_or_404, _site_base


def _sitemap_response(payload: SitemapPayload) -> Response:
    """Build a 200 Response for one sitemap surface.

    Deliberately unconditional (no `Last-Modified` / `ETag`, no
    `make_conditional`): a date-based validator pinned stale
    structural versions in downstream caches. See the module
    docstring for the full why. `payload.last_modified` is retained
    on the dataclass as the sitemap's newest-content-date, it just no
    longer drives an HTTP conditional-GET header.
    """
    return Response(payload.body, mimetype="application/xml; charset=utf-8")


@bp_web.route("/sitemap.xml")
def sitemap() -> Response:
    """Sitemap index. Lists `/meta-sitemap.xml`, one
    `/<inbox>/sitemap.xml` per configured inbox, and
    `/sitemap-maintainers.xml`. Crawlers fetch sub-sitemaps
    independently and can skip unchanged inboxes between visits via
    the per-entry `<lastmod>`. Cached for SITEMAP_TTL_SEC."""
    with SessionLocal() as session:
        payload = sitemap_index_xml(session, _site_base())
    return _sitemap_response(payload)


@bp_web.route("/meta-sitemap.xml")
def meta_sitemap() -> Response:
    """One-entry sitemap listing `/`. Lives behind the sitemap index
    so the index stays purely a `<sitemapindex>` (which can't carry
    a `<url>` for the root directly per sitemaps.org schema). lastmod
    is the global most-recent article date."""
    with SessionLocal() as session:
        payload = meta_sitemap_xml(session, _site_base())
    return _sitemap_response(payload)


@bp_web.route("/sitemap-maintainers.xml")
def maintainers_sitemap() -> Response:
    """Urlset listing every maintainer's profile page
    (`/maintainers/<address>`). No per-url `<lastmod>` (slice-1
    decision, see `maintainers_sitemap_xml`'s docstring), so this
    response never carries `Last-Modified`."""
    with SessionLocal() as session:
        payload = maintainers_sitemap_xml(session, _site_base())
    return _sitemap_response(payload)


@bp_web.route("/<inbox_name>/sitemap.xml")
def inbox_sitemap(inbox_name: str) -> Response:
    """Per-inbox sitemap: dashboard, year + month archives that have
    messages, plus the `SITEMAP_RECENT_PER_INBOX` most-recent article
    URLs. Cached per inbox, so an ingest into one inbox doesn't
    invalidate the others' cached responses."""
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        payload = inbox_sitemap_xml(session, inbox, _site_base())
    return _sitemap_response(payload)
