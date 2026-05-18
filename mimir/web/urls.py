"""URL composition + per-request site-base resolution + small
common 404 helpers used by every route in the package.

The "URL builders" group (`_msg_url`, `_canonical_inbox_name`,
`_canonical_url_for`, `_canonical_inbox_names_for`, `_msg_url`,
`_year_decade_groups`) is consumed by routes, JSON-LD helpers, atom
feeds, and the IndexNow notifier (via package re-exports).

`_site_base` is memoised on `flask.g` because a single message-page
render calls it from the context processor, the route body, and the
JSON-LD helpers; the settings / X-Forwarded-Proto lookups don't need
to repeat per call.
"""
from datetime import datetime, timezone
from email.utils import parseaddr

from flask import abort, g, request
from sqlalchemy import select
from sqlalchemy.orm import Session

from mimir.canonical import fallback_canonical_name
from mimir.config import settings
from mimir.datetime_utils import aware_utc
from mimir.models import Article, ArticleList, Inbox


def _get_inbox_or_404(session: Session, name: str) -> Inbox:
    """Resolve URL slug → Inbox row. Single source of truth for whether
    a `/<inbox_name>/...` URL is valid."""
    inbox = session.execute(
        select(Inbox).where(Inbox.name == name)
    ).scalar_one_or_none()
    if inbox is None:
        abort(404)
    return inbox


def _abort_404_if_url_date_mismatches(article: Article, year: int, month: int) -> None:
    """The URL date is part of the message's identity, not navigation
    state, so a mismatched URL must 404 rather than redirect. Bumps
    the contract from a "fuzzy lookup" to "exact identity match" so
    a URL is either fully resolvable or fully invalid, important for
    the age-at-a-glance property in browser history and shared links.
    Used by the message route and the attachment routes; one helper
    keeps the rule one-place."""
    if article.date is None or year != article.date.year or month != article.date.month:
        abort(404)


def _site_base() -> str:
    """Return the absolute base URL for emitted links, no trailing slash.

    Prefers the explicit `SITE_BASE_URL` setting when set; that's the
    deterministic override for production where ProxyFix may or may
    not be wired correctly across the Tailscale Funnel + Caddy chain.
    Falls back to `request.url_root` for local-dev and any deployment
    that doesn't supply the override; if `X-Forwarded-Proto: https` is
    present but ProxyFix didn't translate it (wrong hop count, header
    not in the trusted set), we still upgrade the scheme. Otherwise
    canonical / og:url / og:image / JSON-LD URLs split between http and
    https on the same page when only one of those signals is wired.

    Memoised on `flask.g` so a message-page render calling this from
    the context processor, the route body, and the JSON-LD helpers
    doesn't repeat the settings / header lookups per call. Bypasses
    memoisation when no request context (CLI render-path tests, etc.).
    """
    from flask import has_request_context
    if has_request_context():
        cached: str | None = getattr(g, "_mimir_site_base", None)
        if cached is not None:
            return cached
    if settings.site_base_url:
        base = settings.site_base_url.rstrip("/")
    else:
        base = request.url_root.rstrip("/")
        if (
            request.headers.get("X-Forwarded-Proto") == "https"
            and base.startswith("http://")
        ):
            base = "https://" + base[len("http://"):]
    if has_request_context():
        g._mimir_site_base = base
    return base


def _msg_url(article: Article, inbox_name: str) -> str:
    """Build the canonical /<list>/YYYY/MM/<id> URL for an Article in
    `inbox_name`. With cross-posts, the same article can render at
    multiple URLs (one per inbox it's linked to); the caller picks
    based on context (the URL's inbox)."""
    if article.date is not None:
        return f"/{inbox_name}/{article.date.year}/{article.date.month:02d}/{article.id}"
    return f"/{inbox_name}/0000/00/{article.id}"


def _canonical_inbox_name(
    article: Article,
    links: list[tuple[int, str]],
) -> str | None:
    """Pick the canonical inbox name for `article` from the list of
    `(inbox_id, inbox_name)` tuples it's linked to. Uses
    `article.canonical_inbox_id` when set; falls back to the
    alphabetically-first link with `Settings.canonical_demoted_inboxes`
    sorted to the back (so a cross-post to lkml + a topical list
    canonicalises to the topical list even before auto-promotion
    populates `canonical_inbox_id`). Stable across renders so the
    SEO signal doesn't flicker between equivalent cross-posts.
    Returns None only when `links` is empty (a corrupt row; should
    never happen given FK cascades)."""
    return fallback_canonical_name(article.canonical_inbox_id, links)


def _year_decade_groups(first_year: int, last_year: int) -> list[tuple[int, list[int]]]:
    """Group `[first_year, last_year]` into decade buckets, newest first.

    Returns a list like `[(2020, [2026, 2025, 2024, ...]), (2010, [2019,
    ..., 2010]), ...]`. Each inner list is descending. Drives the year-
    browse footer on the inbox dashboard; reads better than a flat 30-
    item row on narrow viewports.
    """
    if last_year < first_year:
        return []
    groups: dict[int, list[int]] = {}
    for year in range(last_year, first_year - 1, -1):
        decade = (year // 10) * 10
        groups.setdefault(decade, []).append(year)
    return sorted(groups.items(), key=lambda kv: kv[0], reverse=True)


def _canonical_url_for(
    article: Article,
    links: list[tuple[int, str]],
    base: str = "",
) -> str | None:
    """Compose the full canonical URL for `article` using `links`
    (list of `(inbox_id, inbox_name)`). Returns None when no inbox
    can be resolved."""
    inbox_name = _canonical_inbox_name(article, links)
    if inbox_name is None:
        return None
    return base + _msg_url(article, inbox_name)


def _relative_time(then: datetime, now: datetime | None = None) -> str:
    """Render a coarse relative-time string for the closed-state fold
    summary ("23 messages, 5 authors, 2h ago"). Uses minutes/hours/days
    units under 30 days; falls back to an absolute YYYY-MM-DD beyond
    that, since "47d ago" is harder to parse than the date itself."""
    if now is None:
        now = datetime.now(timezone.utc)
    then = aware_utc(then)
    delta = now - then
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 86400 * 30:
        return f"{secs // 86400}d ago"
    return then.strftime("%Y-%m-%d")


def _thread_summary(thread) -> dict:
    """Compute the headline stats shown in the `closed` fold state:
    total message count, unique-author count (by email so display-name
    drift doesn't fragment the tally), and a coarse relative-time
    string for the most-recent message in the thread."""
    if not thread:
        return {"author_count": 0, "last_activity_rel": "?"}
    emails: set[str] = set()
    for n in thread:
        if not n.author:
            continue
        _, addr = parseaddr(n.author)
        if addr:
            emails.add(addr.lower())
    dates = [n.date for n in thread if n.date]
    last = max(dates) if dates else None
    return {
        "author_count": len(emails) or len(thread),
        "last_activity_rel": _relative_time(last) if last else "?",
    }


def _canonical_inbox_names_for(
    session: Session, article_ids: list[int],
) -> dict[int, str]:
    """Resolve article_id → canonical inbox name for a batch (typically
    a feed's worth, ≤50). Uses `canonical_inbox_id` when set; falls
    back to the alphabetically-first linked inbox with
    `Settings.canonical_demoted_inboxes` sorted to the back. Total
    ≤2 queries regardless of batch size."""
    if not article_ids:
        return {}
    out: dict[int, str] = {}
    rows = session.execute(
        select(Article.id, Inbox.name)
        .join(Inbox, Article.canonical_inbox_id == Inbox.id)
        .where(Article.id.in_(article_ids))
    ).all()
    for art_id, inbox_name in rows:
        out[art_id] = inbox_name
    missing = [aid for aid in article_ids if aid not in out]
    if missing:
        # Pull every linked inbox for the missing set and bucket in
        # Python rather than expressing the two-tier order in a SQL
        # CASE. For a ≤50-article feed with 1-3 inboxes each, the
        # extra rows pulled are negligible and the call-site clarity
        # win is worth it.
        link_rows = session.execute(
            select(ArticleList.article_id, Inbox.name)
            .join(Inbox, Inbox.id == ArticleList.inbox_id)
            .where(ArticleList.article_id.in_(missing))
        ).all()
        demoted_names = frozenset(settings.canonical_demoted_inboxes)
        names_by_article: dict[int, list[str]] = {}
        for art_id, name in link_rows:
            names_by_article.setdefault(art_id, []).append(name)
        for art_id, names in names_by_article.items():
            non_demoted = sorted(n for n in names if n not in demoted_names)
            out[art_id] = non_demoted[0] if non_demoted else min(names)
    return out
