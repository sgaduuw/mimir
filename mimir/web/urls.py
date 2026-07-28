"""URL composition + per-request site-base resolution + small
common 404 helpers used by every route in the package.

The "URL builders" group (`_msg_url`, `_canonical_inbox_name`,
`_canonical_url_for`, `_canonical_inbox_names_for`,
`_year_decade_groups`) is consumed by routes, JSON-LD helpers, atom
feeds, and the IndexNow notifier (via package re-exports).

`_site_base` is memoised on `flask.g` because a single message-page
render calls it from the context processor, the route body, and the
JSON-LD helpers; the settings / X-Forwarded-Proto lookups don't need
to repeat per call.
"""

from flask import abort, g, request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mimir.canonical import fallback_canonical_name
from mimir.config import settings
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
        # Gate the scheme upgrade on `request.is_secure` rather than
        # the raw `X-Forwarded-Proto` header so the trust matches the
        # `TRUSTED_PROXY_HOPS` posture. With ProxyFix wired, is_secure
        # picks up the trusted forwarded scheme; with ProxyFix off
        # (the default), a forged header can no longer flip og:url to
        # `https://` on an HTTP-only deploy.
        if request.is_secure and base.startswith("http://"):
            base = "https://" + base[len("http://") :]
    if has_request_context():
        g._mimir_site_base = base
    return base


def _msg_url(article: Article, inbox_name: str) -> str:
    """Build the canonical /<list>/YYYY/MM/<id> URL for an Article in
    `inbox_name`. With cross-posts, the same article can render at
    multiple URLs (one per inbox it's linked to); the caller picks
    based on context (the URL's inbox)."""
    if article.date is not None:
        return (
            f"/{inbox_name}/{article.date.year}/{article.date.month:02d}/{article.id}"
        )
    return f"/{inbox_name}/0000/00/{article.id}"


def _thread_view_url(article: Article, inbox_name: str) -> str:
    """Whole-thread URL for a thread ROOT: the root's message URL plus
    a `/t` suffix. Keeps the date segments (so the age-at-a-glance
    property of the URL scheme survives) and adds one path token rather
    than a new top-level namespace. Callers are responsible for passing
    a root; `/t` on a reply 301s to the root's own thread view."""
    return _msg_url(article, inbox_name) + "/t"


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


def _canonical_inbox_names_for(
    session: Session,
    article_ids: list[int],
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


def _advertised_urls_for(
    session: Session,
    articles: list[Article],
    base: str = "",
) -> dict[int, str]:
    """`article_id -> the URL to ADVERTISE for that article's conversation.`

    For surfaces that push or publish URLs to machines: IndexNow, atom
    feed entry links, and the `ItemList` JSON-LD on the index and inbox
    pages. Returns the thread view when the article's thread has more
    than one message in its canonical inbox, and the plain message URL
    otherwise.

    Deliberately a DIFFERENT question from the one
    `mimir.web.routes.message` answers when it decides what a page
    claims as its own `<link rel="canonical">`, and the difference is
    worth stating because the two look like they should be one rule:

    - A *page* cedes its canonical to the thread view only when the
      thread view actually CONTAINS it, so it also checks the message's
      position against `thread_view_render_cap`. A reply past the cap is
      linked, not inlined, so it keeps its own canonical.
    - An *advertising* surface names the entry point for a
      conversation, and does not need containment. It matches what the
      sitemap already does: list one URL per conversation and no
      individual replies at all.

    They agree everywhere except a past-cap reply, where this returns
    the thread URL while the reply's own page still self-canonicalises.
    That is the same trade the sitemap already makes, and the
    alternative costs a full ordered thread walk per article on the
    ingest path to compute a position that only changes the answer for
    a rare case.

    Mid-backfill safety: an article whose `thread_root_id` is still
    NULL falls back to its message URL. Never wrong, just less
    consolidated until the backfill reaches it.

    Bounded queries: four regardless of batch size, all keyed on
    indexed columns.
    """
    if not articles:
        return {}
    by_id = {a.id: a for a in articles}
    canonical_names = _canonical_inbox_names_for(session, list(by_id))
    if not canonical_names:
        return {}

    inbox_ids = {
        name: ix_id
        for ix_id, name in session.execute(
            select(Inbox.id, Inbox.name).where(
                Inbox.name.in_(set(canonical_names.values()))
            )
        ).all()
    }

    # The article's root IN ITS CANONICAL INBOX. Threading is
    # inbox-scoped, so reading the root from any other inbox's row
    # would be a different thread; pairs are matched explicitly rather
    # than filtering on article_id alone.
    wanted = {
        (art_id, inbox_ids[name])
        for art_id, name in canonical_names.items()
        if name in inbox_ids
    }
    if not wanted:
        return {}
    root_by_pair: dict[tuple[int, int], int] = {}
    rows = session.execute(
        select(
            ArticleList.article_id, ArticleList.inbox_id, ArticleList.thread_root_id
        ).where(
            ArticleList.article_id.in_({a for a, _ix in wanted}),
            ArticleList.inbox_id.in_({ix for _a, ix in wanted}),
            ArticleList.thread_root_id.is_not(None),
        )
    ).all()
    for art_id, ix_id, root_id in rows:
        if (art_id, ix_id) in wanted:
            root_by_pair[(art_id, ix_id)] = root_id

    # How many messages hang off each root, in the inbox that matters.
    sizes: dict[tuple[int, int], int] = {}
    if root_by_pair:
        size_rows = session.execute(
            select(
                ArticleList.inbox_id,
                ArticleList.thread_root_id,
                func.count(),
            )
            .where(
                ArticleList.thread_root_id.in_(set(root_by_pair.values())),
                ArticleList.inbox_id.in_({ix for _a, ix in root_by_pair}),
            )
            .group_by(ArticleList.inbox_id, ArticleList.thread_root_id)
        ).all()
        for ix_id, root_id, n in size_rows:
            sizes[(ix_id, root_id)] = n

    multi_roots = {
        root_id
        for (_art, ix_id), root_id in root_by_pair.items()
        if sizes.get((ix_id, root_id), 1) > 1
    }
    root_articles = {
        a.id: a
        for a in session.execute(
            select(Article).where(Article.id.in_(multi_roots))
        ).scalars()
    }

    out: dict[int, str] = {}
    for art_id, name in canonical_names.items():
        article = by_id.get(art_id)
        if article is None or article.date is None:
            continue
        ix_id = inbox_ids.get(name)
        root_id = root_by_pair.get((art_id, ix_id)) if ix_id is not None else None
        root = root_articles.get(root_id) if root_id is not None else None
        if root is not None and root.date is not None:
            out[art_id] = base + _thread_view_url(root, name)
        else:
            out[art_id] = base + _msg_url(article, name)
    return out
