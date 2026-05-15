"""SEO output: sitemap XML, JSON-LD payloads, Atom feeds.

Pulled out of `mimir.web` to keep the routes module focused on
request handling. The routes themselves stay in `web.py`; this
module owns the serialisation shape of every structured-data
surface a crawler or feed reader sees.

A few JSON-LD / Atom helpers reach back into `mimir.web` for the
shared display filters (`_msg_url`, `_display_name_filter`,
`_redact_trailer_address`). Those imports are done inside the
function bodies to avoid an import-time cycle (web imports
sitemap/JSON-LD builders from this module at module load).

Cache keys are stable: bumping `cache.NAMESPACE_VERSION`
invalidates everything if a payload shape changes, so per-route
expiry is purely about freshness, not correctness.
"""
import logging
from datetime import datetime, timezone
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, tostring

from flask import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mimir import cache
from mimir.config import settings
from mimir.dashboard import ArticleSummary
from mimir.models import Article, ArticleList, Inbox
from mimir.parser import ParsedArticle
from mimir.rendering import redact_trailer_addresses

logger = logging.getLogger(__name__)

JSON_LD_TEXT_MAX = 2000
SITEMAP_RECENT_PER_INBOX = 5000
SITEMAP_TTL_SEC = 3600

# Tag-URI host portion (RFC 4151). Constant rather than the request
# host so feed entry IDs stay stable across re-deployments and aren't
# tied to whatever proxy the request came in through.
_TAG_URI_AUTHORITY = "mimir"

# Site-wide tagline. Mirrored verbatim by base.html's default
# meta_description block so the WebSite JSON-LD and the meta tag
# can't drift. If you change one, change the other.
DEFAULT_SITE_DESCRIPTION = (
    "Indexed mailing-list archives, served from local "
    "public-inbox v2 mirrors."
)


# Sitemap rendering


def _build_sitemap_xml(entries: list[tuple[str, str | None]]) -> str:
    """Render an XML <urlset> sitemap. Each entry is
    `(loc, lastmod | None)`; when `lastmod` is None the element is
    omitted. Caller formats the timestamp — date-only `YYYY-MM-DD`
    is what Google's docs recommend for crawl-scheduling and is what
    mimir emits."""
    root = Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for loc, lastmod in entries:
        url_el = SubElement(root, "url")
        SubElement(url_el, "loc").text = loc
        if lastmod:
            SubElement(url_el, "lastmod").text = lastmod
    return '<?xml version="1.0" encoding="utf-8"?>\n' + tostring(root, encoding="unicode")


def _build_sitemap_index_xml(entries: list[tuple[str, str | None]]) -> str:
    """Render a <sitemapindex> referencing sub-sitemaps. Same
    `(loc, lastmod)` shape as `_build_sitemap_xml`; the schema and
    element names differ — `<sitemapindex>` of `<sitemap>` rather
    than `<urlset>` of `<url>`."""
    root = Element(
        "sitemapindex", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
    )
    for loc, lastmod in entries:
        sm = SubElement(root, "sitemap")
        SubElement(sm, "loc").text = loc
        if lastmod:
            SubElement(sm, "lastmod").text = lastmod
    return '<?xml version="1.0" encoding="utf-8"?>\n' + tostring(root, encoding="unicode")


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


def sitemap_index_xml(session: Session, base: str, *, force: bool = False) -> str:
    """Cached body of `/sitemap.xml`. Lists `/meta-sitemap.xml` plus one
    `/<inbox>/sitemap.xml` per configured inbox. Same cache key as the
    route uses (`sitemap:index`) so `warm-cache` can pre-populate it
    via `force=True`."""
    def compute() -> str:
        per_inbox_latest = _per_inbox_latest_date(session)
        global_latest = max(
            (d for d in per_inbox_latest.values() if d), default=None
        )
        entries: list[tuple[str, str | None]] = [
            (f"{base}/meta-sitemap.xml", global_latest),
        ]
        inboxes = session.execute(
            select(Inbox).order_by(Inbox.name)
        ).scalars().all()
        for inbox in inboxes:
            entries.append((
                f"{base}/{inbox.name}/sitemap.xml",
                per_inbox_latest.get(inbox.name),
            ))
        return _build_sitemap_index_xml(entries)

    return cache.get_or_compute(
        session, "sitemap:index", SITEMAP_TTL_SEC, compute, force=force,
    )


def meta_sitemap_xml(session: Session, base: str, *, force: bool = False) -> str:
    """Cached body of `/meta-sitemap.xml`. One-URL urlset covering `/`
    with the global-max article date as lastmod."""
    def compute() -> str:
        per_inbox_latest = _per_inbox_latest_date(session)
        global_latest = max(
            (d for d in per_inbox_latest.values() if d), default=None
        )
        return _build_sitemap_xml([(base + "/", global_latest)])

    return cache.get_or_compute(
        session, "sitemap:meta", SITEMAP_TTL_SEC, compute, force=force,
    )


def inbox_sitemap_xml(
    session: Session, inbox: Inbox, base: str, *, force: bool = False
) -> str:
    """Cached body of `/<inbox>/sitemap.xml`. Dashboard + year/month
    archives that actually have data + the SITEMAP_RECENT_PER_INBOX
    most-recent article URLs in this inbox."""
    def compute() -> str:
        entries: list[tuple[str, str | None]] = []

        inbox_latest_dt = session.scalar(
            select(func.max(Article.date))
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date.is_not(None),
            )
        )
        inbox_latest = (
            inbox_latest_dt.strftime("%Y-%m-%d") if inbox_latest_dt else None
        )
        entries.append((f"{base}/{inbox.name}/", inbox_latest))

        # Distinct (year, month) pairs that actually have data, in
        # one round-trip. Empty months are skipped — they'd 200
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

        # Recent articles in the inbox — one URL per article at
        # the inbox's own URL. No canonical-fallback dance:
        # cross-posted articles will appear in each linked
        # inbox's sitemap, which is correct (each is a real,
        # crawlable URL — the canonical `<link>` on the page
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
            entries.append((
                f"{base}/{inbox.name}/{date.year}/{date.month:02d}/{art_id}",
                date.strftime("%Y-%m-%d"),
            ))
        return _build_sitemap_xml(entries)

    return cache.get_or_compute(
        session, f"sitemap:inbox:{inbox.name}", SITEMAP_TTL_SEC, compute,
        force=force,
    )


# JSON-LD payload builders


def _json_ld_index(base: str, inboxes=()) -> dict:
    """schema.org WebSite for the meta-index `/`, paired with an
    `ItemList` of configured inboxes so search engines can treat the
    page as a topical hub rather than a flat link list. Sitelinks-
    search-box is intentionally omitted (mimir's search is per-inbox,
    not site-wide)."""
    payload: dict = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": settings.site_name,
        "url": base + "/",
        "description": DEFAULT_SITE_DESCRIPTION,
    }
    if inboxes:
        payload["mainEntity"] = {
            "@type": "ItemList",
            "name": f"Inboxes indexed by {settings.site_name}",
            "numberOfItems": len(inboxes),
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": i + 1,
                    "url": f"{base}/{inbox.name}/",
                    "name": inbox.name,
                }
                for i, inbox in enumerate(inboxes)
            ],
        }
    return payload


def _json_ld_inbox(base: str, inbox, active_threads=()) -> dict:
    """schema.org payload for `/<inbox_name>/` — a `DiscussionForum`
    container plus an `ItemList` of the currently-most-active threads
    so the page reads as a topical hub for crawlers. `active_threads`
    is whatever the dashboard fetched (root-level ThreadNode objects);
    we project just the bits search engines care about (URL + name).
    """
    # Lazy imports break a `web → seo → web` cycle: these helpers
    # live in web.py with the rest of the display filters.
    from mimir.web import _clean_subject_filter, _msg_url
    payload: dict = {
        "@context": "https://schema.org",
        "@type": "DiscussionForum",
        "name": inbox.name,
        "url": f"{base}/{inbox.name}/",
    }
    if active_threads:
        payload["mainEntity"] = {
            "@type": "ItemList",
            "name": f"Most active threads in {inbox.name}",
            "numberOfItems": len(active_threads),
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": i + 1,
                    "url": f"{base}{_msg_url(t, inbox.name)}",
                    "name": _clean_subject_filter(t.subject) or "(no subject)",
                }
                for i, t in enumerate(active_threads)
            ],
        }
    return payload


def _json_ld_text_snippet(body: str | None) -> str | None:
    """Return a plaintext snippet of `body` suitable for the
    DiscussionForumPosting `text` field, or None when there's nothing
    usable. Whitespace is collapsed (mail bodies have lots of hard
    wraps that read as paragraph noise in JSON-LD); truncation
    happens at the last whitespace inside JSON_LD_TEXT_MAX so we
    don't slice mid-word, with a trailing ellipsis when we did
    truncate. Returning None lets the caller omit the field
    entirely — emitting an empty string would re-fail Google's
    "either text/image/video" validator."""
    if not body:
        return None
    collapsed = " ".join(body.split())
    if not collapsed:
        return None
    if len(collapsed) <= JSON_LD_TEXT_MAX:
        return collapsed
    head = collapsed[:JSON_LD_TEXT_MAX]
    cut = head.rfind(" ")
    # Pathological no-space body: hard-cut at the limit rather than
    # returning the entire string just because rfind didn't find a
    # break point.
    if cut <= 0:
        cut = JSON_LD_TEXT_MAX
    return head[:cut].rstrip() + "..."


def _json_ld_message(
    article: Article,
    parsed: ParsedArticle,
    canonical_url: str,
    inbox_name: str,
    base: str,
) -> dict:
    """schema.org @graph carrying both DiscussionForumPosting (the
    primary signal — eligible for Google's "Discussions and forums"
    rich-result section) and BreadcrumbList (surfaces the
    Site → Inbox → Subject chain in SERPs).

    Author goes through `_display_name_filter` — display name only,
    no email and no `<hidden>` placeholder. The placeholder is a
    rendering decision for the visible HTML; in machine-readable
    metadata it reads as broken data and was flagged as such in the
    2026-05-12 review. `author.url` points at the per-inbox author
    view so the Person has a stable target for "more posts by this
    author"; required-by-Google for the Discussions rich-result
    eligibility (non-critical, Search Console 2026-05-14).
    `dateModified` mirrors `datePublished` because mimir doesn't
    track edits.

    `text` carries a plain-text snippet of `parsed.body`, capped at
    JSON_LD_TEXT_MAX chars (truncated at the last whitespace inside
    the cap) — Google's DiscussionForumPosting validator treats one
    of `text` / `image` / `video` as required (critical, Search
    Console 2026-05-14). Omitted entirely when the body is missing
    or whitespace-only: an empty string would re-fail the validator.

    Prefers `parsed.date` (the original RFC 5322 Date header) over
    `article.date` (the public-inbox commit time) — the message's
    actual send date is more meaningful to search engines."""
    # Lazy imports break the `web → seo → web` cycle (see module
    # docstring). The redaction helpers and display filter live in
    # web.py with the rest of the visible-HTML pipeline.
    from mimir.web import _display_name_filter, _redact_trailer_address
    raw_date = parsed.date or article.date
    if raw_date is not None and raw_date.tzinfo is None:
        # `-0000` Date headers come back tz-naive from
        # parsedate_to_datetime; emit aware UTC so consumers don't
        # see schema-invalid bare datetimes.
        raw_date = raw_date.replace(tzinfo=timezone.utc)
    iso_date = (
        raw_date.strftime("%Y-%m-%dT%H:%M:%S%z") if raw_date else None
    )
    subject = parsed.subject or "(no subject)"
    breadcrumb_subject = subject if len(subject) <= 80 else subject[:77] + "..."
    author_name = _display_name_filter(parsed.author)
    author: dict = {"@type": "Person", "name": author_name}
    # Per-inbox author view is a substring match on the From field;
    # the display name is exactly what'll match the author's other
    # posts. Skip the URL when we fell back to "unknown sender" —
    # that token doesn't match anyone.
    if author_name and author_name != "unknown sender":
        author["url"] = f"{base}/{inbox_name}/author/{quote(author_name, safe='')}"
    forum_post: dict = {
        "@type": "DiscussionForumPosting",
        "@id": canonical_url,
        "url": canonical_url,
        "mainEntityOfPage": canonical_url,
        "headline": subject,
        "author": author,
        "isPartOf": {
            "@type": "WebSite",
            "name": inbox_name,
            "url": f"{base}/{inbox_name}/",
        },
    }
    # Apply the same DCO trailer redaction the visible HTML uses
    # before snippeting — JSON-LD `text` is yet another surface a
    # crawler scrapes, and CONTEXT.md's redaction invariants treat
    # every surface uniformly. Without this, non-allowlisted
    # Signed-off-by addresses would leak through the structured
    # data even though the rendered page redacts them.
    redacted_body = (
        redact_trailer_addresses(parsed.body, _redact_trailer_address)
        if parsed.body else parsed.body
    )
    body_snippet = _json_ld_text_snippet(redacted_body)
    if body_snippet:
        forum_post["text"] = body_snippet
    if iso_date:
        forum_post["datePublished"] = iso_date
        forum_post["dateModified"] = iso_date
    return {
        "@context": "https://schema.org",
        "@graph": [
            forum_post,
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": 1,
                        "name": settings.site_name,
                        "item": base + "/",
                    },
                    {
                        "@type": "ListItem",
                        "position": 2,
                        "name": inbox_name,
                        "item": f"{base}/{inbox_name}/",
                    },
                    {
                        "@type": "ListItem",
                        "position": 3,
                        "name": breadcrumb_subject,
                        "item": canonical_url,
                    },
                ],
            },
        ],
    }


def _json_ld_search(
    base: str, inbox_name: str, query: str, canonical_url: str,
) -> dict:
    """schema.org `SearchResultsPage` for `/<inbox_name>/search?q=…`.
    Emitted only when the route is rendering actual results (the
    no-query / too-short forms are just a search box, not a results
    page). `url` mirrors the canonical, which strips the query
    string — same SEO posture as the `<link rel="canonical">`: this
    is the page-shape, not the result-set."""
    return {
        "@context": "https://schema.org",
        "@type": "SearchResultsPage",
        "name": f"Search results for '{query}' in {inbox_name}",
        "url": canonical_url,
        "description": f"Search results for '{query}' in {inbox_name}.",
        "isPartOf": {
            "@type": "WebSite",
            "name": inbox_name,
            "url": f"{base}/{inbox_name}/",
        },
    }


def _json_ld_author(
    base: str, inbox_name: str, sub: str, canonical_url: str,
) -> dict:
    """schema.org `ProfilePage` for `/<inbox_name>/author/<sub>`. The
    `mainEntity` is a `Person` whose `name` is the sender substring
    we matched against — usually a full email or a domain like
    `@kernel.org`, sometimes a personal display-name fragment. We
    don't try to resolve it to a single identity (the substring may
    match many people, deliberately so for `@kernel.org`-shaped
    queries); `name` is the literal token the page is indexed against.
    """
    return {
        "@context": "https://schema.org",
        "@type": "ProfilePage",
        "name": f"Messages from {sub} in {inbox_name}",
        "url": canonical_url,
        "description": (
            f"Recent messages from senders matching '{sub}' "
            f"in the {inbox_name} archive."
        ),
        "mainEntity": {
            "@type": "Person",
            "name": sub,
        },
        "isPartOf": {
            "@type": "WebSite",
            "name": inbox_name,
            "url": f"{base}/{inbox_name}/",
        },
    }


# Atom feed rendering


def atom_response(
    *,
    feed_id: str,
    feed_title: str,
    self_url: str,
    alternate_url: str,
    entries: list[ArticleSummary],
    inbox_name: str,
    base_url: str,
    canonical_inbox_by_article: dict[int, str] | None = None,
) -> Response:
    """Render an Atom 1.0 feed from a list of `ArticleSummary`. Uses
    stdlib ElementTree — no extra dep. Emits redacted authors via the
    same `safe_from` rule the HTML side uses, so private email
    addresses don't leak via feed readers either.

    `canonical_inbox_by_article` maps article.id → canonical inbox name.
    Cross-posts get their `<id>` and `<link>` set to the canonical URL
    so feed readers that key on `<id>` deduplicate across feeds (the
    same article appearing in lkml's and linux-fsdevel's feeds renders
    as one entry, not two)."""
    # Lazy imports break the `web → seo → web` cycle (see module
    # docstring).
    from mimir.web import _display_name_filter, _msg_url
    feed_updated = (
        max((e.date for e in entries if e.date), default=None)
        or datetime.now(timezone.utc)
    )
    canonical_map = canonical_inbox_by_article or {}

    feed = Element("feed", xmlns="http://www.w3.org/2005/Atom")
    SubElement(feed, "id").text = feed_id
    SubElement(feed, "title").text = feed_title
    SubElement(feed, "updated").text = feed_updated.strftime("%Y-%m-%dT%H:%M:%SZ")
    SubElement(feed, "link", rel="self", type="application/atom+xml", href=self_url)
    SubElement(feed, "link", rel="alternate", type="text/html", href=alternate_url)
    SubElement(feed, "generator").text = "mimir"

    msg_base = base_url.rstrip("/")
    for a in entries:
        entry = SubElement(feed, "entry")
        # RFC 4151 tag URI. Use the canonical inbox name in the tag so
        # cross-posted entries collapse to a single id across feeds —
        # readers that key on <id> won't show duplicates.
        date_str = a.date.strftime("%Y-%m-%d") if a.date else "1970-01-01"
        canonical_inbox_name = canonical_map.get(a.id, inbox_name)
        SubElement(entry, "id").text = (
            f"tag:{_TAG_URI_AUTHORITY},{date_str}:{canonical_inbox_name}/{a.id}"
        )
        SubElement(entry, "title").text = a.subject or "(no subject)"
        if a.date is not None:
            SubElement(entry, "updated").text = a.date.strftime("%Y-%m-%dT%H:%M:%SZ")
        SubElement(
            entry, "link",
            rel="alternate", type="text/html",
            href=msg_base + _msg_url(a, canonical_inbox_name),
        )
        if a.author:
            author_el = SubElement(entry, "author")
            # Display name only — same posture as JSON-LD's author.name.
            # Feed readers render <author><name> as the byline; the
            # `<hidden>` placeholder reads as broken metadata there
            # exactly as it did in JSON-LD before the 2026-05-12 fix.
            SubElement(author_el, "name").text = _display_name_filter(a.author)

    body = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        + tostring(feed, encoding="unicode")
    )
    return Response(body, mimetype="application/atom+xml; charset=utf-8")
