"""The message detail page: thread tree + body + headers + attachments +
subsystem header + related patches + mainline applications + patch-
series timeline.

This is the densest read path in the app; everything happens inside
one SessionLocal so the render hits a single DB connection. The
HTMX intra-thread swap returns the `_message_body.html` partial when
`HX-Request: true` so clicks within the tree don't re-fetch the
chrome.
"""

import hashlib
import logging

from flask import Response, abort, make_response, render_template, request
from sqlalchemy import select

import mimir
from mimir import cache
from mimir.canonical import extract_list_addresses
from mimir.extensions import SessionLocal
from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    Inbox,
)
from mimir.lifecycle_status import lifecycle_status_for_articles
from mimir.patch_state import patch_state_for_article
from mimir.related import is_bot_sender, related_discussions
from mimir.rendering import URL_OR_MSGID_RE
from mimir.rendering.linkify import _extract_lore_msgid
from mimir.seo import _json_ld_message
from mimir.store import MessageNotFound, read_message
from mimir.subsystems import recent_patches_touching, subsystems_for_article
from mimir.threading import find_thread_root, get_thread
from mimir.web._blueprint import bp_web
from mimir.web.filters import _thread_summary
from mimir.web.urls import (
    _abort_404_if_url_date_mismatches,
    _canonical_inbox_name,
    _canonical_url_for,
    _get_inbox_or_404,
    _msg_url,
    _site_base,
)

logger = logging.getLogger(__name__)


# Hard cap on the distinct-Message-ID sets that feed the in-body
# linkifier's two bulk-SELECTs. Typical messages carry <10 refs;
# the cap is well above any real ask. Past it the additional refs
# render as plain text rather than linked, a graceful degradation
# that bounds DB work + memory at render time without any
# user-visible loss on real content.
MSGID_LINKIFY_CAP = 100


@bp_web.route("/<inbox_name>/<int:year>/<int:month>/<int:article_id>")
def message(inbox_name: str, year: int, month: int, article_id: int):
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        article = session.get(Article, article_id)
        if article is None:
            abort(404)
        # Article must be linked to this inbox (cross-posts get one row per inbox).
        linked = session.execute(
            select(ArticleList.article_id).where(
                ArticleList.article_id == article_id,
                ArticleList.inbox_id == inbox.id,
            )
        ).scalar_one_or_none()
        if linked is None:
            abort(404)

        _abort_404_if_url_date_mismatches(article, year, month)

        # Full thread context (replaces the v1 parent + immediate-replies
        # view). Walk constrained to this article's inbox. Loaded BEFORE
        # the body fetch so the ETag-revalidation block below has the
        # freshness signal (max thread date) without the git-mirror blob
        # read; on a 304 we skip both the body fetch and the render.
        root_msgid = (
            find_thread_root(session, inbox, article.message_id) or article.message_id
        )
        thread = get_thread(session, inbox, root_msgid)

        # Conditional-GET (ETag) for the message page. Inputs:
        # - article.id: invariant for the resource itself.
        # - mimir.__version__: invalidates every cached page on deploy so
        #   a UI rendering change (sidebar, headers, layout) surfaces
        #   without waiting for the Cache-Control window to lapse.
        # - thread max date: invalidates when a new reply lands in the
        #   thread, so the tree-rendered view doesn't go stale.
        # - HX-Request flag: the partial response (`_message_body.html`)
        #   is a strict subset of the full response, so the two variants
        #   need distinct ETags. Companion `Vary: HX-Request` header is
        #   applied in `web.hooks` so intermediate caches don't confuse
        #   the two responses for the same URL.
        # Cache-Control on this endpoint is `public, no-cache` (set in
        # `web.hooks._CACHE_CONTROL_BY_ENDPOINT`), so browsers always
        # revalidate; the 304 path skips the body fetch + render entirely.
        hx_request = request.headers.get("HX-Request") == "true"
        thread_max_date = max(
            (n.date for n in thread if n.date is not None),
            default=article.date,
        )
        etag_input = (
            f"{article.id}|{mimir.__version__}|"
            f"{thread_max_date.isoformat() if thread_max_date else ''}|"
            f"{'hx' if hx_request else 'full'}"
        )
        etag = hashlib.blake2s(etag_input.encode(), digest_size=8).hexdigest()
        if etag in request.if_none_match:
            response = Response(status=304)
            response.set_etag(etag)
            return response

        try:
            parsed = read_message(session, inbox, article.message_id)
        except MessageNotFound:
            abort(404)

        # If the thread root still has a thread_parent (i.e. our walk-up hit
        # the top of what we have, but the original chain continued through
        # a message not in this archive), surface that as an "off-list"
        # hint in the UI. Common for cross-posted threads from oss-security,
        # linux-arm-kernel, etc.
        parent_off_list: str | None = None
        if thread and thread[0].thread_parent:
            in_db = session.execute(
                select(Article.id)
                .join(ArticleList, ArticleList.article_id == Article.id)
                .where(
                    ArticleList.inbox_id == inbox.id,
                    Article.message_id == thread[0].thread_parent,
                )
            ).scalar_one_or_none()
            if in_db is None:
                parent_off_list = thread[0].thread_parent

        # When the parent is off-list, surface To/Cc list-shaped addresses
        # that don't map to any configured inbox as a "hint", those are
        # candidate mailing lists the operator might want to add to mimir
        # to recover the missing parent. Strictly a hint: the parent could
        # also simply predate the indexed window. Capped at 3 to avoid a
        # wall of addresses on heavily cross-posted threads.
        parent_off_list_hints: list[str] = []
        if parent_off_list:
            candidates = extract_list_addresses(parsed.headers)
            if candidates:
                known = {
                    addr.lower()
                    for (addr,) in session.execute(
                        select(Inbox.list_address).where(
                            Inbox.list_address.is_not(None)
                        )
                    ).all()
                    if addr
                }
                parent_off_list_hints = [a for a in candidates if a not in known][:3]

        # Non-patch gate for the related-discussions panel (#71):
        # a thread counts as a patch thread when ANY of its articles
        # touches files. Rides the (article_id, path) PK.
        thread_ids = {n.id for n in thread}
        is_patch_thread = (
            session.execute(
                select(ArticleFile.article_id)
                .where(ArticleFile.article_id.in_(list(thread_ids)))
                .limit(1)
            ).first()
            is not None
        )

        # A bot-authored root (syzbot, kernel test robot, tip-bot)
        # shows no panel: the non-patch population is dominated by
        # bot reports whose related matches are low-value (#71).
        # Gated before compute like is_patch_thread so suppressed
        # roots don't enter the empty-rate metric.
        is_bot_root = bool(thread) and is_bot_sender(thread[0].author)

        related_threads: list = []
        if not is_patch_thread and not is_bot_root and thread:
            # Best-effort: a panel bug must never 500 a message view.
            # ERROR (not warning) so a broken scorer stays loud.
            try:
                related_threads = related_discussions(
                    session,
                    inbox,
                    root_id=thread[0].id,
                    thread_article_ids=thread_ids,
                    thread_authors={n.author for n in thread if n.author},
                )
            except Exception:
                logger.exception(
                    "related-discussions failed for %s/%s",
                    inbox.name,
                    article.id,
                )

        # Same-subject orphans within this inbox.
        related: list[Article] = []
        if parent_off_list and article.subject_normalized and is_patch_thread:
            in_thread = {n.message_id for n in thread}
            related = list(
                session.execute(
                    select(Article)
                    .join(ArticleList, ArticleList.article_id == Article.id)
                    .where(
                        ArticleList.inbox_id == inbox.id,
                        Article.subject_normalized == article.subject_normalized,
                        Article.message_id.not_in(in_thread),
                    )
                    .order_by(Article.date.desc().nulls_last())
                    .limit(5)
                ).scalars()
            )

        # Build URLs for thread nodes (avoids per-node template logic).
        thread_urls = {
            n.message_id: f"/{inbox.name}/{n.date.year}/{n.date.month:02d}/{n.id}"
            if n.date
            else f"/{inbox.name}/0000/00/{n.id}"
            for n in thread
        }

        # Resolve in-body <Message-ID> references to canonical URLs.
        # Restrict to this inbox, cross-list refs render as plain text.
        msgid_urls: dict[str, str] = {}
        # In-body `https://lore.kernel.org/<slug>/<msgid>/...` URLs:
        # if mimir has the referenced message in *any* inbox, append
        # a `(local)` mirror link routed to the canonical-inbox URL.
        # Distinct from `msgid_urls` because:
        # 1. Scope is global (cross-inbox); the existing `<MID>`
        #    linkifier is deliberately current-inbox-only per
        #    CONTEXT.md's "references that reach into another inbox
        #    render as plain text". Lore URLs already carry an
        #    explicit cross-list intent (slug in the path), so
        #    routing via canonical-inbox is the appropriate
        #    counterpart.
        # 2. URLs and msgid-refs render differently (the lore URL
        #    survives as-is; only an additional `(local)` anchor is
        #    appended), so keying both off one dict would coerce
        #    semantics that aren't actually the same.
        lore_mirror_urls: dict[str, str] = {}
        if parsed.body:
            # Cap the distinct-Message-ID set both passes feed into
            # the bulk SELECT below. A crafted body could in principle
            # produce thousands of distinct refs inside the parser's
            # body-size budget; each one becomes a `.in_(...)` param
            # against `articles`. The cap bounds DB work + memory at
            # rendering time. Past it, additional Message-IDs render
            # as plain text rather than as linkified refs — a graceful
            # degradation that no real reader will notice (typical
            # messages carry <10 refs).
            candidates: set[str] = set()
            for m in URL_OR_MSGID_RE.finditer(parsed.body):
                if m.group("msgid"):
                    candidates.add(m.group("msgid"))
                    if len(candidates) >= MSGID_LINKIFY_CAP:
                        break
            if candidates:
                referenced = (
                    session.execute(
                        select(Article)
                        .join(ArticleList, ArticleList.article_id == Article.id)
                        .where(
                            ArticleList.inbox_id == inbox.id,
                            Article.message_id.in_(candidates),
                        )
                    )
                    .scalars()
                    .all()
                )
                msgid_urls = {a.message_id: _msg_url(a, inbox.name) for a in referenced}

            lore_msgids: set[str] = set()
            for m in URL_OR_MSGID_RE.finditer(parsed.body):
                if not m.group("url"):
                    continue
                url = m.group("url").rstrip(".,;:!?")
                mid = _extract_lore_msgid(url)
                if mid:
                    lore_msgids.add(mid)
                if len(lore_msgids) >= MSGID_LINKIFY_CAP:
                    break
            if lore_msgids:
                lore_articles = (
                    session.execute(
                        select(Article).where(Article.message_id.in_(lore_msgids))
                    )
                    .scalars()
                    .all()
                )
                # Bulk-load inbox links for canonical-inbox resolution
                # so we don't N+1 the per-article URL composition.
                lore_article_ids = [a.id for a in lore_articles]
                link_rows = list(
                    session.execute(
                        select(ArticleList.article_id, Inbox.id, Inbox.name)
                        .join(Inbox, Inbox.id == ArticleList.inbox_id)
                        .where(ArticleList.article_id.in_(lore_article_ids))
                        .order_by(ArticleList.article_id, Inbox.name)
                    ).all()
                )
                links_by_article: dict[int, list[tuple[int, str]]] = {}
                for art_id, ix_id, ix_name in link_rows:
                    links_by_article.setdefault(art_id, []).append((ix_id, ix_name))
                for art in lore_articles:
                    links = links_by_article.get(art.id)
                    if not links:
                        continue
                    target_inbox = _canonical_inbox_name(art, links) or links[0][1]
                    lore_mirror_urls[art.message_id] = _msg_url(art, target_inbox)

        # All inboxes this article is linked to. Used for both the
        # cross-post hint (which excludes the current inbox) and the
        # canonical URL (which picks one across the full set).
        all_links: list[tuple[int, str]] = list(
            session.execute(
                select(Inbox.id, Inbox.name)
                .join(ArticleList, ArticleList.inbox_id == Inbox.id)
                .where(ArticleList.article_id == article.id)
                .order_by(Inbox.name)
            ).all()
        )
        cross_post_inboxes = [n for ix_id, n in all_links if ix_id != inbox.id]
        base = _site_base()
        canonical_url = _canonical_url_for(article, all_links, base=base)
        # Canonical inbox is what JSON-LD's isPartOf and the breadcrumb
        # should reflect, not necessarily the current URL's inbox.
        canonical_inbox_name = _canonical_inbox_name(article, all_links) or inbox.name
        # Resolved before the JSON-LD build (rather than with the rest
        # of the patch-page surfaces below) because `about` / `keywords`
        # carry the subsystem names. Same query either way.
        subsystem_hits = subsystems_for_article(session, article.id)
        # Replies to THIS message, not the whole thread: the
        # DiscussionForumPosting entity is the single message, so the
        # thread total would be inaccurate on every reply page.
        #
        # Only counted when rendering the canonical inbox. `get_thread`
        # is scoped to the REQUESTED inbox while the emitted entity's
        # `@id` is the CANONICAL inbox's URL, so a cross-posted article
        # whose replies landed in only one of its inboxes would
        # otherwise describe one `@id` with two different reply counts
        # depending on which URL the crawler fetched. Zero omits the
        # field, which is the honest answer from a non-canonical
        # rendering.
        direct_reply_count = (
            sum(1 for n in thread if n.thread_parent == article.message_id)
            if canonical_inbox_name == inbox.name
            else 0
        )
        page_json_ld = (
            _json_ld_message(
                article,
                parsed,
                canonical_url or "",
                canonical_inbox_name,
                base,
                reply_count=direct_reply_count,
                subsystem_names=[h.name for h in subsystem_hits],
            )
            if canonical_url
            else None
        )

        # Subsystem header + related patches + mainline applications +
        # patch-series timeline. Inlined into the route's main session
        # so a single connection covers the whole render, opening a
        # second SessionLocal here previously cost an extra connect /
        # WAL-snapshot acquire per message page.
        touched_paths = list(
            session.execute(
                select(ArticleFile.path).where(ArticleFile.article_id == article.id)
            ).scalars()
        )
        # `recent_patches_touching` is a `path IN (...)` join over
        # `article_files × articles` that gets reused across every
        # render of this article. Cache it; new ingest activity on
        # the same paths surfaces within the 5-minute TTL.
        related_patches = (
            cache.get_or_compute(
                session,
                f"msg_related:{article.id}",
                300,
                lambda: recent_patches_touching(
                    session,
                    touched_paths,
                    exclude_article_id=article.id,
                    limit=5,
                ),
            )
            if touched_paths
            else []
        )
        # Per-patch state card (#208): consolidates trailer roll-up,
        # mainline-landing record, patch-series timeline + per-revision
        # diff links, and thread-activity summary into one card. The
        # helper subsumes the prior `mainline_applications` query and
        # the `patch_series_revisions` block; both `mainline_commits`
        # and the chained-selectinload `patch_series_key` query now
        # live in `mimir.patch_state` for one-place ownership.
        patch_state = patch_state_for_article(
            session,
            article,
            thread_dates=[n.date for n in thread],
            subsystem_ids=[s.id for s in subsystem_hits],
            inbox_name=inbox.name,
        )

        lifecycle_status_by_id = lifecycle_status_for_articles(session, [article.id])
        lifecycle_status = lifecycle_status_by_id.get(article.id)

    # Summary line for the closed-state fold ("23 messages, 5 authors, 2h ago").
    thread_summary = _thread_summary(thread)

    # Parent URL for the hunk-anchored quote renderer: the message
    # this article replies to. When the parent is in-scope (i.e. in
    # the same archive and surfaced in the thread tree), `thread_urls`
    # carries its URL. Off-list parents have no URL and the renderer
    # falls back to a plain <details> without the jump link.
    parent_url: str | None = None
    if article.thread_parent and article.thread_parent in thread_urls:
        parent_url = thread_urls[article.thread_parent]

    # HTMX intra-thread swap: when the click came from a tree link, return
    # only the message-body partial (just the <article id="msg">). The
    # surrounding tree + nav stay put on the client; the client-side script
    # flips which <li> carries the .is-active class after the swap.
    template = "_message_body.html" if hx_request else "message.html"
    response = make_response(
        render_template(
            template,
            inbox_name=inbox.name,
            current_inbox=inbox.name,
            parsed=parsed,
            article=article,
            thread=thread,
            thread_urls=thread_urls,
            parent_url=parent_url,
            thread_summary=thread_summary,
            msgid_urls=msgid_urls,
            lore_mirror_urls=lore_mirror_urls,
            parent_off_list=parent_off_list,
            parent_off_list_hints=parent_off_list_hints,
            related=related,
            related_threads=related_threads,
            cross_post_inboxes=cross_post_inboxes,
            canonical_url=canonical_url,
            page_json_ld=page_json_ld,
            subsystem_hits=subsystem_hits,
            related_patches=related_patches,
            patch_state=patch_state,
            lifecycle_status=lifecycle_status,
        )
    )
    response.set_etag(etag)
    return response
