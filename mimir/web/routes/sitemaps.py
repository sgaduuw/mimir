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

Each response carries a `Last-Modified` header derived from the
sitemap's most-recent content date, and honours `If-Modified-Since`
for conditional GETs (returning 304 Not Modified when the resource
hasn't advanced past the requested timestamp). Without these headers
Google has no cheap way to know the sitemap changed and deprioritises
re-fetching; with them, a crawler that already saw today's content
gets a 304 and mimir avoids regenerating an unchanged response.
"""

from datetime import datetime, timezone

from flask import Response, request

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
    """Build a Response for one sitemap surface. Sets `Last-Modified`
    when the payload carries a date, then calls
    `response.make_conditional(request)` which:

    - Honours `If-Modified-Since`: returns 304 (no body) when the
      request's date is greater-than-or-equal-to the response's
      `Last-Modified`.
    - Adds an automatic ETag if none is set, so `If-None-Match`
      also works.

    Payloads with `last_modified=None` (corner case: a sitemap covering
    an empty inbox or one with no datable content) skip the header,
    in which case crawlers see a normal 200 every time.
    """
    response = Response(payload.body, mimetype="application/xml; charset=utf-8")
    if payload.last_modified:
        # Parse YYYY-MM-DD as start-of-day UTC. Sitemap content has
        # daily granularity; matching the header to the body's
        # `<lastmod>` keeps the two surfaces consistent.
        response.last_modified = datetime.strptime(
            payload.last_modified, "%Y-%m-%d"
        ).replace(tzinfo=timezone.utc)
        response.make_conditional(request)
    return response


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
