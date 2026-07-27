"""Sitemap XML for crawlers.

Four cached surfaces feeding `/sitemap.xml`, `/meta-sitemap.xml`,
`/sitemap-maintainers.xml`, and `/<inbox>/sitemap.xml`. Cache keys are
stable; bumping `cache.NAMESPACE_VERSION` invalidates everything if a
payload shape changes, so per-route expiry is purely about freshness.

Each surface returns a `SitemapPayload` carrying both the XML body and
the most-recent `<lastmod>` date represented in that body. Route
handlers project the date into the HTTP `Last-Modified` header and
honour `If-Modified-Since` for conditional GETs, so crawlers (Google
in particular) can re-fetch the sitemap on a real change rather than
relying on body-content compare which they deprioritise.
"""

from dataclasses import dataclass
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, tostring

from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.orm import Session

from mimir import cache
from mimir.maintainer_directory import all_maintainers
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


def _recent_thread_roots_query(inbox: Inbox):
    """`(article_id, date)` for this inbox's thread roots, newest first.

    Inbox membership is a JOIN, the root test a correlated EXISTS, and
    that split is measured rather than assumed.

    Making inbox membership an EXISTS too lets the planner drive
    `ix_articles_date` DESC and stop at the LIMIT, which is ~8x faster
    on the single dominant inbox (lkml). It is ~37x SLOWER on a small
    one, because the walk can never reach the LIMIT and so scans the
    date index to exhaustion, probing two correlated subqueries per
    row, at a cost that scales with the WHOLE corpus rather than the
    inbox. Production is ~200 inboxes of which ~199 are small, and this
    runs per inbox on every hourly sitemap warm, so the JOIN is the
    right trade by a wide margin (benchmarked on a skewed 606k-row
    corpus: JOIN ~244 ms + 199 x ~2.7 ms, versus EXISTS ~30 ms + 199 x
    ~100 ms).

    This is the 2.8.0 lesson (MEMORY.md 2026-05-29) with its direction
    inverted: measure against the WORST-shaped inboxes, not the one
    that shows a clean win. W8 (materialised thread roots) removes the
    tradeoff entirely by making the root test a column comparison.
    """
    parent = aliased(Article)
    parent_in_inbox = (
        select(ArticleList.article_id)
        .join(parent, parent.id == ArticleList.article_id)
        .where(
            ArticleList.inbox_id == inbox.id,
            parent.message_id == Article.thread_parent,
        )
        .exists()
    )
    return (
        select(Article.id, Article.date)
        .join(ArticleList, ArticleList.article_id == Article.id)
        .where(
            ArticleList.inbox_id == inbox.id,
            Article.date.is_not(None),
            or_(Article.thread_parent.is_(None), ~parent_in_inbox),
        )
        .order_by(Article.date.desc())
    )


def _singleton_root_ids(session, inbox: Inbox, root_ids: list[int]) -> set[int]:
    """Of `root_ids`, those whose thread is just themselves in this
    inbox (no reply hangs off them here).

    Correlated EXISTS, not a join, and the difference is not cosmetic.
    As a join the planner stops driving from `articles IN (ids)` past a
    few hundred ids and drives from `ix_article_lists_inbox_id`
    instead, scanning every article-list row in the inbox and probing
    back, i.e. O(ids x inbox rows). Measured at 3950 ms for 5000 ids
    over a 50k-row inbox and fitting `ids x rows x 1.6e-5 ms` across
    corpus sizes, which extrapolates to minutes per sitemap render on
    lkml. `SITEMAP_RECENT_PER_INBOX` is 5000, so every busy inbox hits
    the worst case exactly, on every hourly warm and on any crawler
    cache miss. The EXISTS form is 5 ms on the same input.

    Same shape and the same reason as the sibling
    `_recent_thread_roots_query`, and as CONTEXT.md's
    `_subsystem_path_filter_exists_sql` note: EXISTS when the outer set
    is already narrowed and the inner test is a per-candidate probe.
    """
    if not root_ids:
        return set()
    child = aliased(Article)
    has_reply = (
        select(child.id)
        .join(ArticleList, ArticleList.article_id == child.id)
        .where(
            child.thread_parent == Article.message_id,
            ArticleList.inbox_id == inbox.id,
            child.id != Article.id,
        )
        .exists()
    )
    with_replies = set(
        session.execute(select(Article.id).where(Article.id.in_(root_ids), has_reply))
        .scalars()
        .all()
    )
    return set(root_ids) - with_replies


def sitemap_index_xml(
    session: Session, base: str, *, force: bool = False
) -> SitemapPayload:
    """Cached body of `/sitemap.xml`. Lists `/meta-sitemap.xml`, one
    `/<inbox>/sitemap.xml` per configured inbox, and
    `/sitemap-maintainers.xml`. Same cache key as the route uses
    (`sitemap:index`) so `warm-cache` can pre-populate it via
    `force=True`. The `last_modified` field of the returned
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
        # Reuse global_latest as the maintainers urlset's lastmod: it's
        # free (already computed above) and a reasonable proxy for
        # "maintainer-relevant activity changed" without an extra
        # per-maintainer date query.
        entries.append((f"{base}/sitemap-maintainers.xml", global_latest))
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


def maintainers_sitemap_xml(
    session: Session, base: str, *, force: bool = False
) -> SitemapPayload:
    """Cached body of `/sitemap-maintainers.xml`. One-urlset listing
    every maintainer's profile page (`/maintainers/<address>`).

    Slice-1 decision: no per-url `<lastmod>` and `last_modified=None`
    on the returned payload, so the route emits a plain 200 with no
    `Last-Modified` header. `all_maintainers` doesn't carry a
    per-maintainer "last active" date, and deriving one would mean a
    date query per maintainer (~4000 on the production MAINTAINERS
    corpus) just to populate a freshness hint crawlers treat as
    secondary to discovery. Revisit if maintainer profile pages ever
    gain a cheap last-changed signal."""

    def compute() -> SitemapPayload:
        entries: list[tuple[str, str | None]] = [
            (f"{base}/maintainers/{quote(addr, safe='@')}", None)
            for addr, _name in all_maintainers(session)
        ]
        return SitemapPayload(body=_build_sitemap_xml(entries), last_modified=None)

    return cache.get_or_compute(
        session,
        "sitemap:maintainers",
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

        # Recent THREADS in the inbox, one URL per thread, pointing at
        # the whole-thread view. Individual message pages canonicalise
        # there (see `mimir.web.routes.message`), and a sitemap should
        # list canonical URLs, so listing per-message URLs here would
        # hand crawlers ~10x the URLs only to redirect their attention
        # elsewhere. One fat thread document per entry is also the
        # whole point of the consolidation.
        #
        # "Is a thread root" is a per-row EXISTS, not the recursive
        # walk-up `find_thread_root` does: a message is a root in this
        # inbox when it has no `thread_parent`, or when that parent
        # isn't in this inbox (an off-list ancestor, which
        # `find_thread_root` also treats as the top).
        #
        # Inbox membership is a JOIN and the root test a correlated
        # EXISTS; see `_recent_thread_roots_query` for the measurements
        # behind that split (the all-EXISTS form is much faster on lkml
        # and much slower on the ~199 small inboxes).
        #
        # Cross-posted articles still appear in each linked inbox's
        # sitemap; the page's own canonical resolves which to keep.
        recent_roots = session.execute(
            _recent_thread_roots_query(inbox).limit(SITEMAP_RECENT_PER_INBOX)
        ).all()
        # Which URL to list has to match what that page's own canonical
        # says, or the sitemap publishes a page that disclaims itself.
        # A single-message thread's canonical is the MESSAGE page (it is
        # already the whole conversation, and carries the patch surfaces
        # `/t` omits), so listing `/t` there would publish the thinner
        # of two self-canonical near-duplicates and drop the canonical
        # one entirely. Mirrors the `len(thread) > 1` rule in
        # `mimir.web.routes.message`.
        singleton_ids = _singleton_root_ids(
            session, inbox, [art_id for art_id, _ in recent_roots]
        )
        for art_id, date in recent_roots:
            # `<lastmod>` is the thread's START date, not its latest
            # activity: deriving the latter needs the batched recursive
            # walk-up `active_threads` uses, which CONTEXT.md measures
            # at ~700 ms for a 12k-message window and which would run
            # here over the whole corpus. `<lastmod>` can only delay a
            # re-crawl, never pin one (the 3.6.1 reasoning), so an
            # understated date on a long-running thread costs freshness
            # rather than correctness. Deep + fresher coverage is W2's
            # year-segmented sitemaps.
            loc = f"{base}/{inbox.name}/{date.year}/{date.month:02d}/{art_id}"
            if art_id not in singleton_ids:
                loc += "/t"
            entries.append((loc, date.strftime("%Y-%m-%d")))
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
