"""Sitemap XML for crawlers.

Three cached surfaces feeding `/sitemap.xml`, `/meta-sitemap.xml`, and
`/<inbox>/sitemap.xml`. Cache keys are stable; bumping
`cache.NAMESPACE_VERSION` invalidates everything if a payload shape
changes, so per-route expiry is purely about freshness.

Each surface returns a `SitemapPayload` carrying both the XML body and
the most-recent `<lastmod>` date represented in that body. Route
handlers project the date into the HTTP `Last-Modified` header and
honour `If-Modified-Since` for conditional GETs, so crawlers (Google
in particular) can re-fetch the sitemap on a real change rather than
relying on body-content compare which they deprioritise.
"""

from dataclasses import dataclass
from xml.etree.ElementTree import Element, SubElement, tostring

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mimir import cache
from mimir.models import Article, ArticleList, Inbox

SITEMAP_RECENT_PER_INBOX = 5000
SITEMAP_TTL_SEC = 3600


@dataclass
class SitemapPayload:
    """The body + last-modified pair for one sitemap surface. The
    last_modified field is an ISO-8601 date string (`YYYY-MM-DD`) or
    None when the sitemap has no datable content. Routes parse it back
    into a datetime to populate `Last-Modified` on the HTTP response.
    The cache (`cache.register("SitemapPayload", ...)` below) stores
    the full dataclass so a warmed body + lastmod are always
    consistent."""

    body: str
    last_modified: str | None


cache.register("SitemapPayload", SitemapPayload)


def _build_sitemap_xml(entries: list[tuple[str, str | None]]) -> str:
    """Render an XML <urlset> sitemap. Each entry is
    `(loc, lastmod | None)`; when `lastmod` is None the element is
    omitted. Caller formats the timestamp, date-only `YYYY-MM-DD`
    is what Google's docs recommend for crawl-scheduling and is what
    mimir emits."""
    root = Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for loc, lastmod in entries:
        url_el = SubElement(root, "url")
        SubElement(url_el, "loc").text = loc
        if lastmod:
            SubElement(url_el, "lastmod").text = lastmod
    return '<?xml version="1.0" encoding="utf-8"?>\n' + tostring(
        root, encoding="unicode"
    )


def _build_sitemap_index_xml(entries: list[tuple[str, str | None]]) -> str:
    """Render a <sitemapindex> referencing sub-sitemaps. Same
    `(loc, lastmod)` shape as `_build_sitemap_xml`; the schema and
    element names differ, `<sitemapindex>` of `<sitemap>` rather
    than `<urlset>` of `<url>`."""
    root = Element("sitemapindex", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for loc, lastmod in entries:
        sm = SubElement(root, "sitemap")
        SubElement(sm, "loc").text = loc
        if lastmod:
            SubElement(sm, "lastmod").text = lastmod
    return '<?xml version="1.0" encoding="utf-8"?>\n' + tostring(
        root, encoding="unicode"
    )


def _per_inbox_latest_date(session) -> dict[str, str | None]:
    """One round-trip: max(article.date) per inbox, as `YYYY-MM-DD`
    strings (or None when the inbox is empty). Feeds the sitemap
    index `<lastmod>` per sub-sitemap."""
    rows = session.execute(
        select(Inbox.name, func.max(Article.date))
        .join(ArticleList, ArticleList.inbox_id == Inbox.id)
        .join(Article, Article.id == ArticleList.article_id)
        .where(Article.date.is_not(None))
        .group_by(Inbox.id)
    ).all()
    return {name: (dt.strftime("%Y-%m-%d") if dt else None) for name, dt in rows}


def sitemap_index_xml(
    session: Session, base: str, *, force: bool = False
) -> SitemapPayload:
    """Cached body of `/sitemap.xml`. Lists `/meta-sitemap.xml` plus one
    `/<inbox>/sitemap.xml` per configured inbox. Same cache key as the
    route uses (`sitemap:index`) so `warm-cache` can pre-populate it
    via `force=True`. The `last_modified` field of the returned
    `SitemapPayload` is the global-max article date (max across all
    per-inbox lastmods), which drives the `Last-Modified` response
    header at the route layer."""

    def compute() -> SitemapPayload:
        per_inbox_latest = _per_inbox_latest_date(session)
        global_latest = max((d for d in per_inbox_latest.values() if d), default=None)
        entries: list[tuple[str, str | None]] = [
            (f"{base}/meta-sitemap.xml", global_latest),
        ]
        inboxes = session.execute(select(Inbox).order_by(Inbox.name)).scalars().all()
        for inbox in inboxes:
            entries.append(
                (
                    f"{base}/{inbox.name}/sitemap.xml",
                    per_inbox_latest.get(inbox.name),
                )
            )
        return SitemapPayload(
            body=_build_sitemap_index_xml(entries),
            last_modified=global_latest,
        )

    return cache.get_or_compute(
        session,
        "sitemap:index",
        SITEMAP_TTL_SEC,
        compute,
        force=force,
    )


def meta_sitemap_xml(
    session: Session, base: str, *, force: bool = False
) -> SitemapPayload:
    """Cached body of `/meta-sitemap.xml`. One-URL urlset covering `/`
    with the global-max article date as lastmod. The `last_modified`
    field carries the same global-max date so the route's
    `Last-Modified` header stays in sync with the body's `<lastmod>`."""

    def compute() -> SitemapPayload:
        per_inbox_latest = _per_inbox_latest_date(session)
        global_latest = max((d for d in per_inbox_latest.values() if d), default=None)
        return SitemapPayload(
            body=_build_sitemap_xml([(base + "/", global_latest)]),
            last_modified=global_latest,
        )

    return cache.get_or_compute(
        session,
        "sitemap:meta",
        SITEMAP_TTL_SEC,
        compute,
        force=force,
    )


def inbox_sitemap_xml(
    session: Session, inbox: Inbox, base: str, *, force: bool = False
) -> SitemapPayload:
    """Cached body of `/<inbox>/sitemap.xml`. Dashboard + year/month
    archives that actually have data + the SITEMAP_RECENT_PER_INBOX
    most-recent article URLs in this inbox. The `last_modified` field
    of the returned `SitemapPayload` is this inbox's most-recent
    article date, scoped so updates to other inboxes don't trigger
    Google to re-fetch this one."""

    def compute() -> SitemapPayload:
        entries: list[tuple[str, str | None]] = []

        inbox_latest_dt = session.scalar(
            select(func.max(Article.date))
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date.is_not(None),
            )
        )
        inbox_latest = inbox_latest_dt.strftime("%Y-%m-%d") if inbox_latest_dt else None
        entries.append((f"{base}/{inbox.name}/", inbox_latest))

        # Distinct (year, month) pairs that actually have data, in
        # one round-trip. Empty months are skipped, they'd 200
        # with a "no messages" page, but the sitemap is for
        # discovery surfaces with real content.
        year_month_rows = session.execute(
            select(
                func.strftime("%Y", Article.date).label("y"),
                func.strftime("%m", Article.date).label("m"),
            )
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date.is_not(None),
            )
            .group_by("y", "m")
            .order_by("y", "m")
        ).all()
        years_with_data: set[str] = {y for y, _ in year_month_rows}
        for y in sorted(years_with_data, reverse=True):
            entries.append((f"{base}/{inbox.name}/{y}/", None))
        for y, m in sorted(year_month_rows, reverse=True):
            entries.append((f"{base}/{inbox.name}/{y}/{m}/", None))

        # Recent articles in the inbox, one URL per article at
        # the inbox's own URL. No canonical-fallback dance:
        # cross-posted articles will appear in each linked
        # inbox's sitemap, which is correct (each is a real,
        # crawlable URL, the canonical `<link>` on the page
        # itself tells search engines which to keep).
        recent = session.execute(
            select(Article.id, Article.date)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date.is_not(None),
            )
            .order_by(Article.date.desc())
            .limit(SITEMAP_RECENT_PER_INBOX)
        ).all()
        for art_id, date in recent:
            entries.append(
                (
                    f"{base}/{inbox.name}/{date.year}/{date.month:02d}/{art_id}",
                    date.strftime("%Y-%m-%d"),
                )
            )
        return SitemapPayload(
            body=_build_sitemap_xml(entries),
            last_modified=inbox_latest,
        )

    return cache.get_or_compute(
        session,
        f"sitemap:inbox:{inbox.name}",
        SITEMAP_TTL_SEC,
        compute,
        force=force,
    )
