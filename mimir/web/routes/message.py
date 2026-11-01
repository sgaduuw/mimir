"""The lean message reader: headers, body, attachments and reply tree.

This is the densest read path in the app; everything happens inside
one SessionLocal so the render hits a single DB connection. The
HTMX intra-thread swap returns the `_message_body.html` partial when
`HX-Request: true` so clicks within the tree don't re-fetch the
chrome.
"""

import hashlib
import logging

from flask import Response, abort, make_response, redirect, render_template, request
from sqlalchemy import select

import mimir
from mimir.canonical import extract_list_addresses
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import (
    Article,
    ArticleList,
    Inbox,
)
from mimir.msgid_url import msgid_from_url
from mimir.rendering import URL_OR_MSGID_RE
from mimir.seo import _json_ld_message
from mimir.store import MessageNotFound, read_message
from mimir.subsystems import subsystems_for_article
from mimir.threading import (
    ThreadNode,
    dedupe_thread,
    find_thread_root,
    get_thread,
    thread_aggregates,
    thread_is_materialised,
    thread_page_of,
    thread_sort_key,
    thread_window,
)
from mimir.web._blueprint import bp_web
from mimir.web.filters import _thread_summary, thread_summary_from
from mimir.web.routes._validators import render_state_tag
from mimir.web.urls import (
    MD_SUFFIX,
    _abort_404_if_url_date_mismatches,
    _canonical_inbox_name,
    _canonical_url_for,
    _get_inbox_or_404,
    _markdown_response,
    _msg_path,
    _msg_url,
    _site_base,
    markdown_path,
    thread_page_url,
)

logger = logging.getLogger(__name__)


# Hard cap on the distinct-Message-ID sets that feed the in-body
# linkifier's two bulk-SELECTs. Typical messages carry <10 refs;
# the cap is well above any real ask. Past it the additional refs
# render as plain text rather than linked, a graceful degradation
# that bounds DB work + memory at render time without any
# user-visible loss on real content.
MSGID_LINKIFY_CAP = 100


@bp_web.route("/<inbox_name>/<int:year>/<int:month>/<int:article_id>" + MD_SUFFIX)
def message_md(inbox_name: str, year: int, month: int, article_id: int):
    """The page as Markdown (#884). The same view rather than a second
    one, so the 404s, the wrong-inbox redirect, the canonical and the
    validator cannot come apart from the HTML's."""
    return message(inbox_name, year, month, article_id, markdown=True)


def _article_by_message_id(session, message_id: str) -> Article:
    return session.execute(
        select(Article).where(Article.message_id == message_id)
    ).scalar_one()


@bp_web.route("/<inbox_name>/<int:year>/<int:month>/<int:article_id>")
def message(
    inbox_name: str, year: int, month: int, article_id: int, markdown: bool = False
):
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        article = session.get(Article, article_id)
        if article is None:
            abort(404)
        # The date is part of the identity, so it is checked BEFORE the
        # inbox: a wrong inbox with a wrong date stays a 404 rather
        # than being redirected into a URL the caller never named.
        _abort_404_if_url_date_mismatches(article, year, month)

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

        # Exists, right date, wrong inbox (cross-posts get one row per
        # inbox): 301 to the canonical message URL (#648). Built by
        # `_canonical_url_for`, the same inbox predicate as the page's
        # canonical tag, so the target is always an inbox the article is
        # in. A self-canonical page's tag equals this Location; a
        # threaded page's tag carries on to its thread page. No links at
        # all is a corrupt row: 404.
        if not any(ix_id == inbox.id for ix_id, _ in all_links):
            target = _canonical_url_for(article, all_links)
            if target is None:
                abort(404)
            return redirect(markdown_path(target) if markdown else target, code=301)

        # Full thread context (replaces the v1 parent + immediate-replies
        # view). Walk constrained to this article's inbox. Loaded BEFORE
        # the body fetch so the ETag-revalidation block below has the
        # freshness signal (max thread date) without the git-mirror blob
        # read; on a 304 we skip both the body fetch and the render.
        root_msgid = (
            find_thread_root(session, inbox, article.message_id) or article.message_id
        )
        # The root by identity, never `thread[0]`: `get_thread` sorts a
        # dateless reply and its subtree before the root, which put the
        # wrong id in the fold pin, the off-list check and the bot gate.
        root = (
            article
            if root_msgid == article.message_id
            else _article_by_message_id(session, root_msgid)
        )
        cap = settings.thread_view_render_cap
        thread_rooted = thread_is_materialised(session, inbox.id, root.id)
        # Named because it is an ETag input, as on the thread view: the
        # column decides whether the page may window the thread at all.
        membership_source = "idx" if thread_rooted else "cte"

        # A thread longer than one thread-view page is windowed around
        # this message instead of rendered whole (#798): the whole tree
        # was 12,342 rows and 4.9 MB on production's largest thread
        # (2026-10-20). Only on the column, because the totals and the
        # thread-view page the gaps link to both come from it.
        agg = thread_aggregates(session, inbox, root.id) if thread_rooted else None
        window = (
            thread_window(session, inbox, root, article)
            if agg is not None and agg.total > cap
            else None
        )
        if window is not None:
            thread = window.rows
            thread_total = agg.total
            thread_max_date = agg.last_activity or article.date
        else:
            # Both fallbacks are data-repair conditions rather than normal
            # reads, logged like the thread view's so they are not
            # indistinguishable from the fast path except by latency.
            if not thread_rooted:
                logger.warning(
                    "message: %s/%s rendered from the recursive walk; "
                    "thread has unrooted members",
                    inbox.name,
                    article.id,
                )
            elif agg is not None and agg.total > cap:
                logger.warning(
                    "message: %s/%s rendered whole; the walk up does not "
                    "reach the materialised root %s",
                    inbox.name,
                    article.id,
                    root.id,
                )
            # Deduped so the thread count matches the rows rendered (see
            # `dedupe_thread`).
            thread = dedupe_thread(get_thread(session, inbox, root_msgid))
            thread_total = len(thread)
            thread_max_date = max(
                (n.date for n in thread if n.date is not None),
                default=article.date,
            )

        base = _site_base()
        canonical_url = _canonical_url_for(article, all_links, base=base)

        # Consolidate onto the whole-thread view when this message is
        # actually rendered there. Most replies are a line or two, so
        # one thread of N messages otherwise presents as N thin,
        # near-duplicate URLs competing with each other; pointing them
        # at the conversation gives search engines one substantial
        # document per thread instead.
        #
        # Conditional on the thread being RANKABLE, not on the
        # message's position: since 3.8.0 the view paginates and
        # truncates nothing, so every message is contained by some
        # page and the canonical names that page rather than page 1.
        # What it cannot do is name a page for a thread whose
        # `thread_root_id` column is incomplete, because the rank would
        # be computed from rows the renderer does not agree with; those
        # threads stay self-canonical per message. The gate below is
        # `thread_is_materialised`, and a canonical pointing at a page
        # that does not contain this content would be a false claim.
        #
        # Built on the canonical inbox, so this composes with (rather
        # than fights) the existing cross-post consolidation: a
        # cross-posted thread still collapses to one inbox first.
        canonical_inbox_name = _canonical_inbox_name(article, all_links) or inbox.name

        # `message_canonical_url` stays the message's own URL and keeps
        # feeding the JSON-LD entity: a DiscussionForumPosting on this
        # page IS this message, so its `@id` must remain the message's
        # URL even once the page's canonical points at the thread.
        # Only the `<link rel="canonical">` (and `og:url`, which
        # follows it in base.html) moves.
        message_canonical_url = canonical_url
        thread_view_url: str | None = None

        # Consolidate onto the thread view, resolved in the inbox the
        # canonical already points at.
        #
        # Doing this in the REQUESTED inbox instead produces a CHAIN on
        # cross-posts: this page would point at its own inbox's thread
        # view while `_canonical_url_for` had already elected a
        # different inbox, so the other arm canonicalises here and here
        # canonicalises onward. Every hop is individually truthful,
        # which is why a per-page check misses it; the composition is
        # the defect, and search engines do not follow chains reliably.
        #
        # Threading is inbox-scoped, so the target inbox's copy of the
        # conversation is a DIFFERENT node set: the root may be absent
        # there, or this message may not be in that inbox's thread. Each
        # is checked against the target inbox's own rows rather than
        # assumed from this inbox's. Anything unmet falls back to the
        # plain cross-post canonical.
        #
        # Asked of the column, not of a walk. The claim being made is
        # that `thread_page_of`'s page holds this message, and that
        # page is built from the column, so the column is what has to
        # agree. Walking the target's thread here cost a second
        # whole-thread walk on every cross-posted page.
        own_page_no: int | None = None
        if canonical_url:
            target_inbox = inbox
            target_root = root
            if canonical_inbox_name != inbox.name:
                target_inbox = session.execute(
                    select(Inbox).where(Inbox.name == canonical_inbox_name)
                ).scalar_one_or_none()
                target_root = None
                if target_inbox is not None:
                    target_root_msgid = find_thread_root(
                        session, target_inbox, article.message_id
                    )
                    if target_root_msgid:
                        target_root = _article_by_message_id(session, target_root_msgid)
            in_target_thread = False
            if target_inbox is not None and target_root is not None:
                # Its own row carries the root. A single-message thread
                # counts too (#1003): every thread's aggregations live on
                # its `/t`, so that is every message's canonical. This
                # matters when the row names a root no longer in the inbox
                # (a `reindex --from-scratch` leftover): `find_thread_root`
                # falls back to the walk and `thread_is_materialised` only
                # looks for NULL rows, but `thread_page_of` would not count
                # this message.
                in_target_thread = (
                    session.execute(
                        select(ArticleList.thread_root_id).where(
                            ArticleList.article_id == article.id,
                            ArticleList.inbox_id == target_inbox.id,
                        )
                    ).scalar_one_or_none()
                    == target_root.id
                )
            if (
                in_target_thread
                and target_root.date is not None
                and thread_is_materialised(session, target_inbox.id, target_root.id)
            ):
                # Canonicalise to the PAGE that contains this message,
                # not to page 1. Since 3.8.0 the thread view is
                # paginated, so every message is rendered somewhere.
                #
                # The page number comes from `thread_page_of`, the same
                # helper the thread view builds its page URLs with, so
                # the canonical and the page cannot disagree about where
                # the message lives.
                page_no = thread_page_of(
                    session, target_inbox.id, target_root.id, article, cap
                )
                if target_inbox is inbox:
                    own_page_no = page_no  # the window's gap links want it too
                thread_view_url = thread_page_url(
                    target_root.id,
                    target_root.date,
                    target_inbox.name,
                    page_no,
                )
                canonical_url = base + thread_view_url

        # Where the window's gaps lead: the thread-view page holding this
        # message in THIS inbox, whose thread the window shows. A
        # dateless root has no thread-view URL, so the gaps stay text.
        thread_more_url: str | None = None
        if window is not None and root.date is not None:
            thread_more_url = thread_page_url(
                root.id,
                root.date,
                inbox.name,
                own_page_no
                if own_page_no is not None
                else thread_page_of(session, inbox.id, root.id, article, cap),
            )

        if thread_more_url is None and root.date is not None:
            thread_more_url = thread_page_url(root.id, root.date, inbox.name)

        # Conditional-GET (ETag) for the message page. Inputs:
        # - article.id: invariant for the resource itself.
        # - mimir.__version__: invalidates every cached page on deploy so
        #   a UI rendering change (sidebar, headers, layout) surfaces
        #   without waiting for the Cache-Control window to lapse.
        # - thread max date: invalidates when a new reply lands in the
        #   thread, so the tree-rendered view doesn't go stale.
        # - thread total: the page shows it ("Thread (N messages)", the
        #   window's footer) and a purge or reindex moves it without
        #   moving the max date.
        # - membership source and window flag: whether the column may
        #   answer for this thread, and so whether it renders whole or
        #   windowed (#798). The cap below is the window's threshold too.
        # - render state tag: retained conservatively after #990. The
        #   visible status panels moved to /t, but subsystem attribution
        #   still feeds JSON-LD and the HTMX OOB nav, and the allowlist
        #   still controls redaction. Removing the whole tag would pin
        #   those remaining outputs behind stale 304s.
        # - HX-Request flag: the partial response (`_message_body.html`)
        #   is a strict subset of the full response, so the two variants
        #   need distinct ETags. Companion `Vary: HX-Request` header is
        #   applied in `web.hooks` so intermediate caches don't confuse
        #   the two responses for the same URL. The Markdown form (#884)
        #   is a third representation on its own URL; it renders a
        #   subset of the full page's inputs, so it shares the content
        #   part.
        # - thread render cap: this page's canonical is
        #   `thread_page_url(..., thread_page_of(..., cap))`, so the cap
        #   decides which thread page the message claims to live on, and
        #   a stale claim 404s once the cap grows. `thread.py` already
        #   folds it in. It is the one input that moves the body while
        #   every other input stands still: it is an operator env knob,
        #   so changing it restarts the same image and the version
        #   component below does not move either.
        # - resolved canonical URL: `<link rel="canonical">` and
        #   `og:url`. It moves without touching this inbox's thread when
        #   the canonical inbox changes (a recompute, a new cross-post
        #   link, the demoted-inbox setting), when the TARGET inbox's
        #   copy of the thread changes, or when a `thread_root_id`
        #   repair flips `thread_is_materialised` (#656). Carrying the
        #   result rather than a list of its inputs is what keeps it
        #   complete when the canonical logic grows another input.
        # - inbox link names: the "Also in:" line renders from them, so a
        #   new cross-post that leaves the canonical in place still
        #   changes the body.
        # Cache-Control on this endpoint is `public, no-cache` (set in
        # `web.hooks._CACHE_CONTROL_BY_ENDPOINT`), so browsers always
        # revalidate; the 304 path skips the body fetch + render entirely.
        hx_request = request.headers.get("HX-Request") == "true"
        state_tag = render_state_tag(session, article.id, article.message_id)
        # Full HTML, HTMX and Markdown remain distinct representations.
        # Derived cards no longer render here (#990).
        content_input = (
            f"{article.id}|{mimir.__version__}|"
            f"{settings.thread_view_render_cap}|"
            f"{thread_max_date.isoformat() if thread_max_date else ''}|"
            f"{thread_total}|{membership_source}|"
            f"{'window' if window is not None else 'whole'}|"
            f"{state_tag}|"
            f"{canonical_url or ''}|"
            f"{','.join(name for _, name in all_links)}"
        )
        representation = "md" if markdown else "hx" if hx_request else "full"
        etag_input = f"{content_input}|{representation}"
        etag = hashlib.blake2s(etag_input.encode(), digest_size=8).hexdigest()
        if etag in request.if_none_match:
            response = Response(status=304)
            response.set_etag(etag)
            return response

        try:
            parsed = read_message(session, inbox, article.message_id)
        except MessageNotFound:
            abort(404)

        if markdown:
            # Header, body and attachments only: everything below this
            # point feeds panels the Markdown form does not render.
            body = render_template(
                "message.md",
                inbox_name=inbox.name,
                parsed=parsed,
                article=article,
                thread_view_url=thread_view_url,
            )
            return _markdown_response(body, etag, canonical_url)

        # If the thread root still has a thread_parent (i.e. our walk-up hit
        # the top of what we have, but the original chain continued through
        # a message not in this archive), surface that as an "off-list"
        # hint in the UI. Common for cross-posted threads from oss-security,
        # linux-arm-kernel, etc.
        parent_off_list: str | None = None
        if root.thread_parent:
            in_db = session.execute(
                select(Article.id)
                .join(ArticleList, ArticleList.article_id == Article.id)
                .where(
                    ArticleList.inbox_id == inbox.id,
                    Article.message_id == root.thread_parent,
                )
            ).scalar_one_or_none()
            if in_db is None:
                parent_off_list = root.thread_parent

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

        # The window's rows include gap rows; everything below that
        # asks about messages asks about these.
        thread_nodes = [n for n in thread if isinstance(n, ThreadNode)]

        # Build URLs for thread nodes (avoids per-node template logic).
        thread_urls = {
            n.message_id: _msg_path(n.id, n.date, inbox.name) for n in thread_nodes
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
                mid = msgid_from_url(url)
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

        cross_post_inboxes = [n for ix_id, n in all_links if ix_id != inbox.id]

        # Resolved before the JSON-LD build (rather than with the rest
        # of the patch-page surfaces below) because `about` / `keywords`
        # carry the subsystem names. Same query either way.
        subsystem_hits = subsystems_for_article(session, article)
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
        if canonical_inbox_name != inbox.name:
            direct_reply_count = 0
        elif window is not None:
            direct_reply_count = window.reply_count
        else:
            direct_reply_count = sum(
                1 for n in thread_nodes if n.thread_parent == article.message_id
            )

        page_json_ld = (
            _json_ld_message(
                article,
                parsed,
                message_canonical_url or "",
                canonical_inbox_name,
                base,
                reply_count=direct_reply_count,
                subsystem_names=[h.name for h in subsystem_hits],
                page_canonical_url=canonical_url,
            )
            if message_canonical_url
            else None
        )

        position = next(
            (i for i, node in enumerate(thread_nodes) if node.id == article.id),
            None,
        )
        previous_url = (
            thread_urls[thread_nodes[position - 1].message_id]
            if position is not None and position > 0 and article.id != root.id
            else None
        )
        next_url = (
            thread_urls[thread_nodes[position + 1].message_id]
            if position is not None and position + 1 < len(thread_nodes)
            else None
        )
        parent = next(
            (node for node in thread_nodes if node.message_id == article.thread_parent),
            None,
        )
        thread_link_url = thread_view_url
        thread_link_anchor = f"#m{article.id}"
        if thread_link_url is None and root.date is not None:
            # A repair fallback still has a thread view; only its canonical
            # claim is withheld. Rank its already-loaded nodes in the same
            # order as /t's recursive fallback, separately from the DFS tree.
            if thread_rooted:
                page_no = thread_page_of(session, inbox.id, root.id, article, cap)
            else:
                rank = next(
                    (
                        i
                        for i, node in enumerate(
                            sorted(thread_nodes, key=thread_sort_key(root.id))
                        )
                        if node.id == article.id
                    ),
                    None,
                )
                page_no = rank // cap + 1 if rank is not None else 1
                if rank is None:
                    thread_link_anchor = ""
            thread_link_url = thread_page_url(root.id, root.date, inbox.name, page_no)
        if thread_link_url:
            thread_link_url += thread_link_anchor

    # Summary line for the closed-state fold ("23 messages, 5 authors, 2h ago").
    thread_summary = (
        thread_summary_from(agg.authors, agg.last_activity, agg.total)
        if window is not None
        else _thread_summary(thread_nodes)
    )

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
            thread_total=thread_total,
            thread_root_id=root.id,
            thread_more_url=thread_more_url,
            hidden_replies=window.hidden_replies if window is not None else {},
            thread_urls=thread_urls,
            parent_url=parent_url,
            parent=parent,
            previous_url=previous_url,
            next_url=next_url,
            thread_link_url=thread_link_url,
            thread_summary=thread_summary,
            msgid_urls=msgid_urls,
            lore_mirror_urls=lore_mirror_urls,
            parent_off_list=parent_off_list,
            parent_off_list_hints=parent_off_list_hints,
            cross_post_inboxes=cross_post_inboxes,
            canonical_url=canonical_url,
            thread_view_url=thread_view_url,
            page_json_ld=page_json_ld,
            subsystem_hits=subsystem_hits,
            markdown_url=markdown_path(_msg_url(article, inbox.name)),
        )
    )
    response.set_etag(etag)
    return response
