"""`/sitemap.xml`, `/messages-sitemap.xml`, `/meta-sitemap.xml`,
`/sitemap-maintainers.xml`, `/sitemap-series-diffs-<h>.xml`,
`/<inbox>/sitemap.xml` and the archive files.

`/sitemap.xml` is a `<sitemapindex>` pointing at `/meta-sitemap.xml`
(a urlset for `/` and `/merge-window`), one `/<inbox>/sitemap.xml` per configured
inbox, `/sitemap-maintainers.xml` (a urlset of every maintainer
profile page), and sixteen `/sitemap-series-diffs-<h>.xml` (the series
cover diffs, one page per first hex character of the key), plus the
per-inbox year and month files of thread URLs. `/messages-sitemap.xml`
is a second, independent index of the same archive files' message
halves (#964). Crawlers fetch the sub-sitemaps independently and
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

from flask import Response, abort

from mimir.extensions import SessionLocal
from mimir.seo import (
    archive_sitemap_xml,
    inbox_sitemap_xml,
    maintainers_sitemap_xml,
    meta_sitemap_xml,
    series_diff_sitemap_xml,
    sitemap_indexes_xml,
)
from mimir.seo.sitemaps import SERIES_DIFF_BUCKETS, SitemapPayload
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
    `/<inbox>/sitemap.xml` per configured inbox,
    `/sitemap-maintainers.xml`, and the series diff pages. Crawlers fetch sub-sitemaps
    independently and can skip unchanged inboxes between visits via
    the per-entry `<lastmod>`. Cached for SITEMAP_TTL_SEC."""
    with SessionLocal() as session:
        payload = sitemap_indexes_xml(session, _site_base())[0]
    return _sitemap_response(payload)


@bp_web.route("/messages-sitemap.xml")
def messages_sitemap() -> Response:
    """Sitemap index of message pages: the `messages-` sibling of every
    archive file `/sitemap.xml` lists (#964). Message pages mostly
    duplicate lore.kernel.org, so they are kept apart from the pages
    mimir adds, and dropping them later is one index and one robots.txt
    line. Built and cached together with `/sitemap.xml`."""
    with SessionLocal() as session:
        payload = sitemap_indexes_xml(session, _site_base())[1]
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
    response never carries `Last-Modified`."""
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
    *,
    messages: bool = False,
) -> Response:
    """One month or year's thread URLs for one inbox, paged, or with
    `messages` its message URLs.

    These files are the only sitemap surface listing thread and message
    pages, every root included (#964).

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
        payload = archive_sitemap_xml(
            session, inbox, year, month, page, _site_base(), messages=messages
        )
    if payload is None:
        abort(404)
    return _sitemap_response(payload)


@bp_web.route("/<inbox_name>/<int:year>/messages-sitemap.xml")
@bp_web.route("/<inbox_name>/<int:year>/messages-sitemap-<int:page>.xml")
@bp_web.route("/<inbox_name>/<int:year>/<int:month>/messages-sitemap.xml")
@bp_web.route("/<inbox_name>/<int:year>/<int:month>/messages-sitemap-<int:page>.xml")
def archive_messages_sitemap(
    inbox_name: str, year: int, month: int | None = None, page: int = 1
) -> Response:
    """The message half of the matching `archive_sitemap` file (#964)."""
    return archive_sitemap(inbox_name, year, month, page, messages=True)
