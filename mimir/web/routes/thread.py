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
canonical (see `mimir.web.urls.thread_view_url_for_message`).
"""

import hashlib
import logging

from flask import Response, abort, make_response, redirect, render_template, request
from sqlalchemy import select

import mimir
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, Inbox
from mimir.seo import _json_ld_thread
from mimir.store import MessageNotFound, read_message
from mimir.subsystems import subsystems_for_article
from mimir.threading import find_thread_root, get_thread
from mimir.web._blueprint import bp_web
from mimir.web.filters import _thread_summary
from mimir.web.urls import (
    _abort_404_if_url_date_mismatches,
    _canonical_inbox_name,
    _get_inbox_or_404,
    _msg_url,
    _site_base,
    _thread_view_url,
)

logger = logging.getLogger(__name__)


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
            if root is not None and root.date is not None:
                return redirect(_thread_view_url(root, inbox.name), code=301)
            root_msgid = article.message_id

        nodes = get_thread(session, inbox, root_msgid)
        if not nodes:
            abort(404)

        # ETag before any blob read, mirroring the message route: on a
        # 304 we skip the whole render, which matters more here than
        # anywhere else in the app (a capped render is up to
        # `thread_view_render_cap` blob fetches). The cap is an input
        # because changing it changes the response body.
        thread_max_date = max(
            (n.date for n in nodes if n.date is not None),
            default=article.date,
        )
        # A cap below 1 would render no messages and then IndexError in
        # the JSON-LD builder (which needs a root), 500ing every thread
        # view. Clamp rather than validate: this is an ops knob and an
        # unusable value should degrade, not take the surface down.
        cap = max(1, settings.thread_view_render_cap)
        etag_input = (
            f"thread|{article.id}|{mimir.__version__}|{cap}|{len(nodes)}|"
            f"{thread_max_date.isoformat() if thread_max_date else ''}"
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

        root_node = nodes[0]
        subsystem_names = [
            h.name for h in subsystems_for_article(session, root_node.id)
        ]

        # This is the ONE place cross-inbox consolidation happens for
        # threads, and it keys on the ROOT's canonical inbox rather
        # than this article's. A fully cross-posted conversation
        # otherwise renders a near-identical page under every inbox it
        # touches, each self-canonical, which would re-introduce at the
        # thread level exactly the duplication this surface exists to
        # remove.
        #
        # Gated on the root actually being linked to that inbox. It
        # always is when `canonical_inbox_id` is set (that column
        # references one of the article's own links), but the fallback
        # ordering can name an inbox for a *different* article, so the
        # membership check is what keeps this from pointing at a 404.
        root_links: list[tuple[int, str]] = list(
            session.execute(
                select(Inbox.id, Inbox.name)
                .join(ArticleList, ArticleList.inbox_id == Inbox.id)
                .where(ArticleList.article_id == root_node.id)
                .order_by(Inbox.name)
            ).all()
        )
        root_article = session.get(Article, root_node.id)
        canonical_inbox = inbox.name
        if root_article is not None:
            picked = _canonical_inbox_name(root_article, root_links)
            if picked and any(name == picked for _id, name in root_links):
                canonical_inbox = picked

        base = _site_base()
        canonical_url = base + _thread_view_url(root_node, canonical_inbox)
        page_json_ld = _json_ld_thread(
            nodes=rendered_nodes,
            parsed_by_id={n.id: p for n, p, _ in messages if p is not None},
            canonical_url=canonical_url,
            inbox_name=inbox.name,
            base=base,
            total_replies=len(nodes) - 1,
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
        )
    )
    response.set_etag(etag)
    return response
