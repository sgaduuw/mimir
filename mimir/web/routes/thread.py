"""The whole-thread view: every message in one conversation on one page.

Additive to the message page, which keeps its own reading model (tree
stacked above a single body). This surface exists because a single
message is a poor unit for an index: most replies are a couple of lines
("Acked-by", "applied, thanks"), so a 60-message thread is 60 thin,
near-duplicate URLs. The conversation as a whole is the substantial,
genuinely unique document, so it is what the sitemap lists and what the
individual message pages canonicalise to.

Rendering is capped at `settings.thread_view_render_cap`; past that the
tail is linked rather than inlined, and those messages keep their own
canonical (see the containment gate in `mimir.web.routes.message`).
"""

import hashlib
import logging

from flask import Response, abort, make_response, redirect, render_template, request
from sqlalchemy import func, select

import mimir
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import (
    Article,
    ArticleList,
    ArticleTrailer,
    MainlineCommit,
    MainlineState,
)
from mimir.lifecycle_status import lifecycle_status_for_articles
from mimir.patch_state import patch_state_for_article
from mimir.seo import _json_ld_thread
from mimir.store import MessageNotFound, read_message
from mimir.subsystems import subsystems_for_article
from mimir.threading import dedupe_thread, find_thread_root, get_thread
from mimir.web._blueprint import bp_web
from mimir.web.filters import _thread_summary
from mimir.web.urls import (
    _abort_404_if_url_date_mismatches,
    _get_inbox_or_404,
    _msg_url,
    _site_base,
    _thread_view_url,
)

logger = logging.getLogger(__name__)


def _render_state_tag(session, root_id: int, root_msgid: str) -> str:
    """Validator input covering everything the page renders ABOUT the
    root that no message carries: its landing state, its review
    roll-up, and its subsystem attribution.

    Three indexed reads rather than a `lifecycle_status_for_articles`
    probe, for two reasons. It has to run BEFORE the 304
    short-circuit (it feeds the validator), and that helper computes a
    recursive CTE and then WRITES a cache row, which in the web tier is
    a broker RPC: the cheap path would have acquired a query, a CTE and
    a write on the single-writer broker, on exactly the URLs the
    sitemap aims crawlers at. And it only covers lifecycle, while the
    page also renders the subsystem line, which moves on any
    `update-mainline` MAINTAINERS reparse (every 10 minutes in prod)
    without touching the node count or the thread's max date.

    `MainlineState.last_commit_sha` is the HEAD at the last MAINTAINERS
    load, so it versions the whole subsystem-rule snapshot in one
    scalar instead of re-deriving this article's matches.
    """
    landings = session.execute(
        select(
            func.count(MainlineCommit.commit_sha),
            func.max(MainlineCommit.committed_at),
        ).where(MainlineCommit.message_id == root_msgid)
    ).one()
    trailers = session.scalar(
        select(func.count(ArticleTrailer.id)).where(
            ArticleTrailer.article_id == root_id
        )
    )
    rules_version = session.scalar(
        select(MainlineState.last_commit_sha).where(MainlineState.tree_name == "linus")
    )
    # Supersedance and the revision count come from SIBLING articles
    # sharing this patch's series key, so they live in neither the
    # landing, trailer, nor rules reads above. Posting a v2 flips this
    # thread's badge to SUPERSEDED and rewrites its synthesis prose to
    # "revision 1 of 2", without adding a message to it. Posting a v2
    # is the most routine patch workflow on the list, and "is this the
    # current revision" is squarely in the query family this surface
    # exists to answer.
    series = session.execute(
        select(Article.patch_series_key, Article.patch_series_position).where(
            Article.id == root_id
        )
    ).one_or_none()
    series_tag = ""
    if series is not None and series[0]:
        count, newest = session.execute(
            select(
                func.count(Article.id),
                func.max(Article.patch_series_version),
            ).where(
                Article.patch_series_key == series[0],
                func.coalesce(Article.patch_series_position, 0) == (series[1] or 0),
            )
        ).one()
        series_tag = f"{count}|{newest or ''}"
    return (
        f"{landings[0]}|{landings[1] or ''}|{trailers or 0}|"
        f"{rules_version or ''}|{series_tag}"
    )


@bp_web.route("/<inbox_name>/<int:year>/<int:month>/<int:article_id>/t")
def thread_view(inbox_name: str, year: int, month: int, article_id: int):
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        article = session.get(Article, article_id)
        if article is None:
            abort(404)
        linked = session.execute(
            select(ArticleList.article_id).where(
                ArticleList.article_id == article_id,
                ArticleList.inbox_id == inbox.id,
            )
        ).scalar_one_or_none()
        if linked is None:
            abort(404)
        _abort_404_if_url_date_mismatches(article, year, month)

        # One thread, one URL. Asking for `/t` on a reply is a coherent
        # request ("show me this conversation"), so redirect to the
        # root's thread view rather than 404 or render a partial thread
        # from the middle. 301 because the mapping is permanent for
        # this reply, and it consolidates crawl signal on the one URL
        # the sitemap lists.
        root_msgid = (
            find_thread_root(session, inbox, article.message_id) or article.message_id
        )
        if root_msgid != article.message_id:
            root = session.execute(
                select(Article)
                .join(ArticleList, ArticleList.article_id == Article.id)
                .where(
                    ArticleList.inbox_id == inbox.id,
                    Article.message_id == root_msgid,
                )
            ).scalar_one_or_none()
            # A root we can't resolve back to a row (or one with no date
            # to build a URL from) leaves nothing to redirect to; render
            # this article's own subtree rather than 404, so the page
            # still works for a reader.
            # Only redirect to a FIXED POINT. `thread_parent` comes
            # straight from sender-controlled `In-Reply-To` with no
            # cycle guard at ingest, so a cyclic thread can yield a
            # root whose own root is a different node again, and
            # redirecting blindly makes `/t` an infinite 301 loop on a
            # no-cache endpoint the sitemap advertises.
            #
            # Still required after W8, though the reason narrowed: for
            # rows with a materialised root the column converges cycle
            # members on one self-rooted member, so the fixed point
            # holds. It is the NULL rows, still answered by the
            # recursive walk (which stops at MAX_DEPTH rather than at a
            # fixed point), that can still produce a non-settling
            # root.
            settles = root is not None and (
                find_thread_root(session, inbox, root.message_id) == root.message_id
            )
            if settles and root.date is not None:
                return redirect(_thread_view_url(root, inbox.name), code=301)
            root_msgid = article.message_id

        nodes = get_thread(session, inbox, root_msgid)
        if not nodes:
            abort(404)
        nodes = dedupe_thread(nodes)

        # ETag before any blob read, mirroring the message route: on a
        # 304 we skip the whole render, which matters more here than
        # anywhere else in the app (a capped render is up to
        # `thread_view_render_cap` blob fetches). The cap is an input
        # because changing it changes the response body.
        thread_max_date = max(
            (n.date for n in nodes if n.date is not None),
            default=article.date,
        )
        root_id_for_etag = next(
            (n.id for n in nodes if n.message_id == root_msgid),
            nodes[0].id,
        )
        # A cap below 1 would render no messages and then IndexError in
        # the JSON-LD builder (which needs a root), 500ing every thread
        # view. Clamp rather than validate: this is an ops knob and an
        # unusable value should degrade, not take the surface down.
        cap = max(1, settings.thread_view_render_cap)
        # Everything the page renders ABOUT the root that no message
        # carries: landing state, review roll-up, subsystem
        # attribution. None of them moves `len(nodes)` or
        # `thread_max_date`, so without this a patch landing (or a
        # MAINTAINERS reparse, every 10 minutes in prod) leaves the
        # edge and every crawler holding a validator that pins the
        # exact "did $series land" text this release made canonical.
        state_tag = _render_state_tag(session, root_id_for_etag, root_msgid)
        etag_input = (
            f"thread|{article.id}|{mimir.__version__}|{cap}|{len(nodes)}|"
            f"{thread_max_date.isoformat() if thread_max_date else ''}|{state_tag}"
        )
        etag = hashlib.blake2s(etag_input.encode(), digest_size=8).hexdigest()
        if etag in request.if_none_match:
            response = Response(status=304)
            response.set_etag(etag)
            return response

        rendered_nodes = nodes[:cap]
        overflow = nodes[cap:]

        # One message per entry: the parsed body for rendering, or None
        # when the blob is unreachable. A single missing blob (a mirror
        # gap, a re-packed epoch) must not 404 the whole conversation,
        # so the node keeps its header row and loses only its body.
        messages: list[tuple] = []
        for node in rendered_nodes:
            try:
                parsed = read_message(session, inbox, node.message_id)
            except MessageNotFound:
                logger.warning(
                    "thread-view: blob unavailable for %s in %s",
                    node.message_id,
                    inbox.name,
                )
                parsed = None
            messages.append((node, parsed, _msg_url(node, inbox.name)))

        overflow_links = [(n, _msg_url(n, inbox.name)) for n in overflow]

        # The root by identity, not by position: `get_thread`'s
        # `sort_path` is NULL for a dateless node and NULL sorts first,
        # so `nodes[0]` is not reliably the root.
        root_node = next(
            (n for n in nodes if n.message_id == root_msgid),
            nodes[0],
        )
        subsystem_hits = subsystems_for_article(session, root_node.id)
        subsystem_names = [h.name for h in subsystem_hits]

        # The root's patch surfaces. Message pages in a multi-message
        # thread canonicalise here, and the message page is otherwise
        # the RICHER document, so without these the consolidation would
        # trade away exactly the lifecycle prose this release added as
        # indexable text for "did $series land" queries, plus the
        # subsystem attribution and review roll-up no other LKML mirror
        # emits. Three reads per thread render, not per message.
        root_article = session.get(Article, root_node.id)
        root_patch_state = None
        root_lifecycle = None
        if root_article is not None:
            root_patch_state = patch_state_for_article(
                session,
                root_article,
                thread_dates=[n.date for n in nodes],
                subsystem_ids=[h.id for h in subsystem_hits],
                inbox_name=inbox.name,
            )
            root_lifecycle = lifecycle_status_for_articles(session, [root_node.id]).get(
                root_node.id
            )

        # Self-canonical, deliberately. Cross-inbox thread
        # consolidation was tried and reverted: `get_thread` is
        # inbox-scoped, so the "same" thread has DIFFERENT membership
        # in each inbox (a reply that trimmed one list from its Cc is
        # simply absent there). Pointing one inbox's thread page at
        # another's therefore hands authority to a page that may omit
        # content this one renders, which is the exact false-containment
        # failure this surface exists to avoid. Gating on the root being
        # present in the target is not sufficient: root membership says
        # nothing about reply membership, and the target may not even
        # treat that article as its root, so the canonical can land on a
        # 301.
        #
        # The cost is that a fully cross-posted conversation renders a
        # near-identical page per inbox. That residual is small (the
        # message-level consolidation already collapsed N messages to 1
        # page per inbox, so this is 2 URLs where there were 2N) and
        # search engines dedupe near-identical pages on their own. A
        # truthful weaker signal beats a stronger false one.
        #
        # W8 does not change this: per-inbox threading is fundamental to
        # the data model, not an artefact of how roots are computed.
        base = _site_base()
        canonical_url = base + _thread_view_url(root_node, inbox.name)
        page_json_ld = _json_ld_thread(
            nodes=rendered_nodes,
            parsed_by_id={n.id: p for n, p, _ in messages if p is not None},
            canonical_url=canonical_url,
            inbox_name=inbox.name,
            base=base,
            total_replies=len(nodes) - 1,
            last_activity=thread_max_date,
            subsystem_names=subsystem_names,
        )

    thread_summary = _thread_summary(nodes)
    response = make_response(
        render_template(
            "thread.html",
            inbox_name=inbox.name,
            current_inbox=inbox.name,
            root=root_node,
            messages=messages,
            overflow_links=overflow_links,
            total_count=len(nodes),
            thread_summary=thread_summary,
            canonical_url=canonical_url,
            page_json_ld=page_json_ld,
            root_url=_msg_url(root_node, inbox.name),
            subsystem_hits=subsystem_hits,
            patch_state=root_patch_state,
            lifecycle_status=root_lifecycle,
        )
    )
    response.set_etag(etag)
    return response
