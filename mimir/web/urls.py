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
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from mimir.canonical import fallback_canonical_name
from mimir.config import settings
from mimir.models import Article, ArticleList, Inbox
from mimir.threading import unmaterialised_roots, thread_page_of


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
    return _msg_path(article.id, article.date, inbox_name)


def _msg_path(article_id: int, date, inbox_name: str) -> str:
    """The `(id, date)` form of `_msg_url`, for callers that have rows
    rather than Articles. Every message-URL spelling in the codebase
    resolves here, so there is one place that decides how a year and a
    month are written into a path."""
    if date is not None:
        return f"/{inbox_name}/{date.year}/{date.month:02d}/{article_id}"
    return f"/{inbox_name}/0000/00/{article_id}"


def _thread_view_url(article: Article, inbox_name: str) -> str:
    """Whole-thread URL for a thread ROOT: the root's message URL plus
    a `/t` suffix. Keeps the date segments (so the age-at-a-glance
    property of the URL scheme survives) and adds one path token rather
    than a new top-level namespace. Callers are responsible for passing
    a root; `/t` on a reply 301s to the root's own thread view."""
    return _msg_url(article, inbox_name) + "/t"


def thread_page_url(article_id: int, date, inbox_name: str, page: int = 1) -> str:
    """One page of a thread view, from a root's `(id, date)` rather than
    an Article.

    Exists because the sitemap does not have Article objects: it works
    from `(id, date)` rows and used to hand-build the same path with its
    own f-string. That made the sitemap's `<loc>` and the page's own
    canonical two parallel implementations of one URL, which agreed only
    because both happened to spell the year unpadded and the month
    `:02d`. Nothing forced them to, and the route accepts BOTH
    spellings, so a change to either would have gone unnoticed while
    every advertised URL disagreed with the canonical of the page it
    points at, i.e. exactly the duplicate-URL signal the canonical
    exists to prevent (CONTEXT.md "Emitter and acceptor must share one
    validity rule").

    Page 1 has no suffix: `/t` and `/t/1` are the same page, and `/t` is
    the spelling every other surface uses.
    """
    path = _msg_path(article_id, date, inbox_name) + "/t"
    return path if page <= 1 else f"{path}/{page}"


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
    """Resolve article_id → canonical inbox name for a batch.

    Delegates the rule to `mimir.canonical.fallback_canonical_name`
    rather than re-deriving it. That matters: the rule is "use
    `canonical_inbox_id` when set AND PRESENT IN LINKS, else the
    alphabetically-first linked inbox with demoted names at the back",
    and this function used to drop the present-in-links half. It is not
    a hypothetical difference. `canonical_inbox_id` is assigned at
    ingest by matching To:/Cc: list addresses against every inbox that
    has a `list_address`, with nothing requiring the article to be
    linked to that inbox, so a message archived only in lkml but
    addressed to a topical list carries that list's id and no row
    there. Resolving to it yields a URL that 404s.

    Two queries regardless of batch size.
    """
    if not article_ids:
        return {}
    canonical_ids = {
        art_id: canon_id
        for art_id, canon_id in session.execute(
            select(Article.id, Article.canonical_inbox_id).where(
                Article.id.in_(article_ids)
            )
        ).all()
    }
    links_by_article: dict[int, list[tuple[int, str]]] = {}
    for art_id, ix_id, name in session.execute(
        select(ArticleList.article_id, Inbox.id, Inbox.name)
        .join(Inbox, Inbox.id == ArticleList.inbox_id)
        .where(ArticleList.article_id.in_(article_ids))
    ).all():
        links_by_article.setdefault(art_id, []).append((ix_id, name))

    out: dict[int, str] = {}
    for art_id in article_ids:
        name = fallback_canonical_name(
            canonical_ids.get(art_id), links_by_article.get(art_id, [])
        )
        if name is not None:
            out[art_id] = name
    return out


def _advertised_urls_for(
    session: Session,
    articles: list[Article],
    base: str = "",
) -> dict[int, str]:
    """`article_id -> the URL to ADVERTISE for that article's conversation.`

    For surfaces that push or publish URLs to machines: the IndexNow
    push today. Returns the thread view when the article's thread has
    more than one message in its canonical inbox, and the plain message
    URL otherwise.

    Deliberately a DIFFERENT question from the one
    `mimir.web.routes.message` answers when it decides what a page
    claims as its own `<link rel="canonical">`:

    - A *page* cedes its canonical to the thread PAGE that contains it.
      Since 3.8.0 the view paginates, so every message is rendered
      somewhere and the old "past the cap keeps its own canonical" rule
      is gone.
    - An *advertising* surface used not to need containment. It does
      now, for the same reason: a reply lands on the LAST page, so
      announcing page 1 named a URL that had not changed while the page
      that grew went unannounced. It matches what the
      sitemap already does: list one URL per conversation and no
      individual replies at all.

    Everything here is keyed on the `(article, inbox)` PAIR, never on
    the article or the root alone. Threading is inbox-scoped, so the
    same root can head a five-message thread in one inbox and a
    single-message one in another; deciding on the root alone let a
    singleton thread inherit a sibling inbox's thread URL and advertise
    a duplicate-content page that no other surface lists.

    Two cases, and only one of them needs to ask the database:

    - The article is NOT its own root, so it is a reply, so the thread
      has at least the root and this article. Multi-message by
      construction.
    - The article IS its own root, so the question is whether anything
      replied to it in this inbox.

    That second test uses the same `thread_parent` EXISTS that
    `mimir.seo.sitemaps._singleton_root_ids` uses, and for the reason
    documented there: counting POPULATED `thread_root_id` rows instead
    undercounts mid-backfill, so a thread whose replies are not yet
    filled reads as a singleton and gets advertised as a message URL
    while its own page canonicalises to the thread view. Two
    advertising surfaces must not disagree about what a singleton is.

    Mid-backfill safety: an article whose own `thread_root_id` is still
    NULL falls back to its message URL, because without a root there is
    no thread URL to name. Less consolidated, never dangling.

    Four queries plus one `thread_page_of` COUNT per article, all
    on indexed columns. It was batch-independent until 3.8.0 made the
    advertised URL page-aware; see `indexnow.py` for the cost.
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
    wanted = {
        (art_id, inbox_ids[name])
        for art_id, name in canonical_names.items()
        if name in inbox_ids
    }
    if not wanted:
        return {}

    # The article's root IN ITS CANONICAL INBOX. Reading it from any
    # other inbox's row would be a different thread, so pairs are
    # matched explicitly rather than filtering on article_id alone.
    root_by_pair: dict[tuple[int, int], int] = {}
    for art_id, ix_id, root_id in session.execute(
        select(
            ArticleList.article_id, ArticleList.inbox_id, ArticleList.thread_root_id
        ).where(
            ArticleList.article_id.in_({a for a, _ix in wanted}),
            ArticleList.inbox_id.in_({ix for _a, ix in wanted}),
            ArticleList.thread_root_id.is_not(None),
        )
    ).all():
        if (art_id, ix_id) in wanted:
            root_by_pair[(art_id, ix_id)] = root_id

    # Only pairs where the article IS the root need asking; a reply is
    # multi-message by construction. That proposition is true and is
    # not the one that matters: what the caller needs is "the thread
    # page URL is servable in this inbox", and those come apart when a
    # root row is gone from the inbox (a partial `reindex
    # --from-scratch`, a hand repair), leaving a reply pointing at a
    # root that is not here. What actually prevents the dangling push
    # is `unmaterialised_roots` condition (1), which covers roots with
    # no row in this inbox at all. Pinned by
    # `test_build_urls_does_not_advertise_a_thread_whose_root_is_gone_from_the_inbox`.
    root_pairs = {
        (ix_id, root_id)
        for (art_id, ix_id), root_id in root_by_pair.items()
        if root_id == art_id
    }
    roots_with_replies: set[tuple[int, int]] = set()
    if root_pairs:
        child = aliased(Article)
        child_link = aliased(ArticleList)
        has_reply = (
            select(child.id)
            .join(child_link, child_link.article_id == child.id)
            .where(
                child.thread_parent == Article.message_id,
                child_link.inbox_id == ArticleList.inbox_id,
                child.id != Article.id,
            )
            .exists()
        )
        for ix_id, art_id in session.execute(
            select(ArticleList.inbox_id, ArticleList.article_id)
            .join(Article, Article.id == ArticleList.article_id)
            .where(
                ArticleList.article_id.in_({r for _ix, r in root_pairs}),
                ArticleList.inbox_id.in_({ix for ix, _r in root_pairs}),
                has_reply,
            )
        ).all():
            roots_with_replies.add((ix_id, art_id))

    consolidated: dict[int, int] = {}
    for (art_id, ix_id), root_id in root_by_pair.items():
        if root_id != art_id or (ix_id, root_id) in roots_with_replies:
            consolidated[art_id] = root_id
    root_articles = {
        a.id: a
        for a in session.execute(
            select(Article).where(Article.id.in_(set(consolidated.values())))
        ).scalars()
    }

    # Which roots the column cannot fully rank, batched per inbox
    # rather than asked per article. A thread with unrooted members is
    # rendered from the walk, so any page number derived from the
    # column is too low for every message after the gap.
    roots_by_inbox: dict[int, set[int]] = {}
    for art_id, name in canonical_names.items():
        ix_id = inbox_ids.get(name)
        root = root_articles.get(consolidated.get(art_id))
        if ix_id is not None and root is not None:
            roots_by_inbox.setdefault(ix_id, set()).add(root.id)
    # Keyed on the `(inbox, root)` PAIR. Root ids are global
    # `articles.id`, so a flat set of them made a root flagged in one
    # inbox match in every other, and a single unrepaired row anywhere
    # dropped a healthy cross-posted thread's whole page-aware
    # advertising. That contradicted this function's own docstring
    # three paragraphs up, which is the shape that keeps hiding these.
    unrankable: set[tuple[int, int]] = set()
    for ix_id, roots in roots_by_inbox.items():
        unrankable |= {
            (ix_id, r) for r in unmaterialised_roots(session, ix_id, list(roots))
        }

    out: dict[int, str] = {}
    for art_id, name in canonical_names.items():
        article = by_id.get(art_id)
        if article is None or article.date is None:
            continue
        root = root_articles.get(consolidated.get(art_id))
        if root is not None and root.date is not None:
            # The PAGE holding this message, not page 1. A new reply
            # lands on the LAST page, so announcing the first one
            # reported a URL that had not changed while the page that
            # actually grew was never announced at all. Same helper the
            # thread view and the message canonical use, so this cannot
            # become a third opinion about where a message lives.
            ix_id = inbox_ids.get(name)
            if ix_id is not None and (ix_id, root.id) in unrankable:
                # The thread has members the column cannot see, so any
                # page number computed from it would be too low. Announce
                # the message's own URL instead of a page that may not
                # contain it: less consolidated, never dangling, which
                # is this function's existing posture for the NULL-root
                # case one branch down.
                out[art_id] = base + _msg_url(article, name)
                continue
            page_no = 1
            if ix_id is not None:
                page_no = thread_page_of(
                    session,
                    ix_id,
                    root.id,
                    article,
                    max(1, settings.thread_view_render_cap),
                )
            out[art_id] = base + thread_page_url(root.id, root.date, name, page_no)
        else:
            out[art_id] = base + _msg_url(article, name)
    return out
