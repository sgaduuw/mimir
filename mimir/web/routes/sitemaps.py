"""`/sitemap.xml`, `/meta-sitemap.xml`,
`/sitemap-maintainers.xml`, `/sitemap-series-diffs-<h>.xml`,
`/<inbox>/sitemap.xml` and the archive files.

`/sitemap.xml` is a `<sitemapindex>` pointing at `/meta-sitemap.xml`
(a urlset for `/` and `/merge-window`), one `/<inbox>/sitemap.xml` per configured
inbox, `/sitemap-maintainers.xml` (a urlset of every maintainer
profile page), and sixteen `/sitemap-series-diffs-<h>.xml` (the series
cover diffs, one page per first hex character of the key), plus the
per-inbox year and month files of thread URLs. Crawlers fetch the
sub-sitemaps independently and skip unchanged inboxes via each entry's
`<lastmod>`. The index/meta
split (issue #10) replaced the earlier single monolithic urlset;
cross-posted articles now appear in each linked inbox's sitemap
(every one is a real URL, and the page's own `<link rel="canonical">`
tells search engines which to keep).

Responses carry an ETag of the exact XML bytes and honour If-None-Match
for GET and HEAD. The tag changes whenever the served body changes,
including structural changes, membership removals and cache fills that
leave the newest mail date unchanged. It stays stable when an unchanged
body is rebuilt after cache expiry.

There is no Last-Modified header, and date-only conditional requests return
200. The newest content date plus a release timestamp cannot date every
same-release change. Durable body-change timestamps are deferred. Per-URL
<lastmod> values remain content signals inside the XML. Cache-Control and
the archive-specific Cloudflare-CDN-Cache-Control retain their existing
lifetimes, so this validator does not shorten cache freshness periods.
"""

from datetime import UTC, datetime

from flask import Response, abort, request

from mimir.extensions import SessionLocal
from mimir.seo import (
    archive_sitemap_xml,
    inbox_sitemap_xml,
    maintainers_sitemap_xml,
    meta_sitemap_xml,
    series_diff_sitemap_xml,
    sitemap_index_xml,
)
from mimir.seo.sitemaps import (
    SERIES_DIFF_BUCKETS,
    WEEK_SEC,
    SitemapPayload,
    archive_sitemap_ttl,
)
from mimir.web._blueprint import bp_web
from mimir.web.urls import _get_inbox_or_404, _site_base


def _sitemap_response(payload: SitemapPayload) -> Response:
    """Validate the served XML bytes, including changes unrelated to mail dates."""
    response = Response(payload.body, mimetype="application/xml; charset=utf-8")
    response.add_etag()
    return response.make_conditional(request)


@bp_web.route("/sitemap.xml")
def sitemap() -> Response:
    """Sitemap index. Lists `/meta-sitemap.xml`, one
    `/<inbox>/sitemap.xml` per configured inbox,
    `/sitemap-maintainers.xml`, and the series diff pages. Crawlers fetch sub-sitemaps
    independently and can skip unchanged inboxes between visits via
    the per-entry `<lastmod>`. Cached for SITEMAP_TTL_SEC."""
    with SessionLocal() as session:
        payload = sitemap_index_xml(session, _site_base())
    return _sitemap_response(payload)


@bp_web.route("/meta-sitemap.xml")
def meta_sitemap() -> Response:
    """Sitemap listing `/` and `/merge-window`. Lives behind the sitemap index
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
    response has a byte-based ETag but no `Last-Modified` header."""
    with SessionLocal() as session:
        payload = maintainers_sitemap_xml(session, _site_base())
    return _sitemap_response(payload)


@bp_web.route("/sitemap-series-diffs-<bucket>.xml")
def series_diff_sitemap(bucket: str) -> Response:
    """Urlset of the cover-letter diffs between adjacent revisions, for
    the series whose key starts with `bucket` (one lowercase hex
    character). Built on request and cached; see
    `series_diff_sitemap_xml`."""
    if len(bucket) != 1 or bucket not in SERIES_DIFF_BUCKETS:
        abort(404)
    with SessionLocal() as session:
        payload = series_diff_sitemap_xml(session, bucket, _site_base())
    return _sitemap_response(payload)


@bp_web.route("/<inbox_name>/sitemap.xml")
def inbox_sitemap(inbox_name: str) -> Response:
    """Per-inbox sitemap of hub pages: dashboard, year + month archives
    that have messages, and the active subsystems. Thread and message
    pages are in the archive files. Cached per inbox, so an ingest into
    one inbox doesn't invalidate the others' cached responses."""
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        payload = inbox_sitemap_xml(session, inbox, _site_base())
    return _sitemap_response(payload)


@bp_web.route("/<inbox_name>/<int:year>/sitemap.xml")
@bp_web.route("/<inbox_name>/<int:year>/sitemap-<int:page>.xml")
@bp_web.route("/<inbox_name>/<int:year>/<int:month>/sitemap.xml")
@bp_web.route("/<inbox_name>/<int:year>/<int:month>/sitemap-<int:page>.xml")
def archive_sitemap(
    inbox_name: str,
    year: int,
    month: int | None = None,
    page: int = 1,
) -> Response:
    """One month or year's thread URLs for one inbox, paged.

    These files are the only sitemap surface listing thread pages (#964).
    A root whose thread the column cannot yet rank is in none of them
    until its backfill reaches it (#1089).

    Validated before it reaches SQL, because both segments arrive from
    the URL. An out-of-range month would otherwise build a nonsense
    date range, and a page beyond the end 404s rather than serving an
    empty urlset a crawler would keep re-fetching.
    """
    # Every segment is attacker-supplied. Beyond the obvious month
    # range: year 0 and year 9999 both raise from `datetime` (the
    # latter because the month range needs `year + 1`), and an
    # astronomically large page number used to overflow when it reached
    # SQLite. All of those were 500s on a public URL.
    if (
        (month is not None and not 1 <= month <= 12)
        or not 1 <= year <= 9998
        or not 1 <= page <= 10_000
    ):
        abort(404)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        payload = archive_sitemap_xml(session, inbox, year, month, page, _site_base())
    if payload is None:
        abort(404)
    response = _sitemap_response(payload)
    # The edge follows the origin TTL, capped at a week: a cache-key bump
    # cannot drop an edge copy, so a month would outlast a release that
    # changes what these files hold (3.6.0). Cloudflare strips this header;
    # clients keep `Cache-Control: max-age=300` from hooks.py.
    edge = min(archive_sitemap_ttl(year, month, datetime.now(UTC).date()), WEEK_SEC)
    response.headers["Cloudflare-CDN-Cache-Control"] = f"max-age={edge}"
    return response


@bp_web.route("/messages-sitemap.xml")
@bp_web.route("/<inbox_name>/<int:year>/messages-sitemap.xml")
@bp_web.route("/<inbox_name>/<int:year>/messages-sitemap-<int:page>.xml")
@bp_web.route("/<inbox_name>/<int:year>/<int:month>/messages-sitemap.xml")
@bp_web.route("/<inbox_name>/<int:year>/<int:month>/messages-sitemap-<int:page>.xml")
def messages_sitemap_gone(**_url_args) -> Response:
    """The retired message sitemaps (#1089). Every file was empty once the
    backfill finished. 410, not 404, so a crawler drops the URL rather
    than retrying it. Nothing validates the segments: a retired URL is
    gone whatever its inbox or date."""
    abort(410)
