"""The message detail page: thread tree + body + headers + attachments +
subsystem header + related patches + mainline applications + patch-
series timeline.

This is the densest read path in the app; everything happens inside
one SessionLocal so the render hits a single DB connection. The
HTMX intra-thread swap returns the `_message_body.html` partial when
`HX-Request: true` so clicks within the tree don't re-fetch the
chrome.
"""
from flask import abort, render_template, request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from mimir import cache
from mimir.canonical import extract_list_addresses
from mimir.extensions import SessionLocal
from mimir.models import (
    Article, ArticleFile, ArticleList, Inbox, MainlineCommit,
)
from mimir.rendering import URL_OR_MSGID_RE
from mimir.seo import _json_ld_message
from mimir.store import MessageNotFound, read_message
from mimir.subsystems import recent_patches_touching, subsystems_for_article
from mimir.threading import find_thread_root, get_thread
from mimir.web._blueprint import bp_web
from mimir.web.urls import (
    _abort_404_if_url_date_mismatches,
    _canonical_inbox_name,
    _canonical_url_for,
    _get_inbox_or_404,
    _msg_url,
    _site_base,
    _thread_summary,
)


# Threshold above which the message-page layout switches from
# "thread tree above body" to "thread tree as a right rail" on
# wide viewports. Picked from issue #68's >~20-message guideline:
# below this, the above-body box is fine; above it, the box's
# height cap ends up paginating most of the tree out of view and
# the rail layout (mutt / Thunderbird / Discourse) is what
# everyone expects. The CSS still falls back to above-body on
# narrow viewports regardless of length.
LONG_THREAD_SIDEBAR_THRESHOLD = 20


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

        try:
            parsed = read_message(session, inbox, article.message_id)
        except MessageNotFound:
            abort(404)

        # Full thread context (replaces the v1 parent + immediate-replies view).
        # Walk constrained to this article's inbox.
        root_msgid = find_thread_root(session, inbox, article.message_id) or article.message_id
        thread = get_thread(session, inbox, root_msgid)

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
                        select(Inbox.list_address).where(Inbox.list_address.is_not(None))
                    ).all()
                    if addr
                }
                parent_off_list_hints = [a for a in candidates if a not in known][:3]

        # Same-subject orphans within this inbox.
        related: list[Article] = []
        if parent_off_list and article.subject_normalized:
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
        if parsed.body:
            candidates = {
                m.group("msgid")
                for m in URL_OR_MSGID_RE.finditer(parsed.body)
                if m.group("msgid")
            }
            if candidates:
                referenced = session.execute(
                    select(Article)
                    .join(ArticleList, ArticleList.article_id == Article.id)
                    .where(
                        ArticleList.inbox_id == inbox.id,
                        Article.message_id.in_(candidates),
                    )
                ).scalars().all()
                msgid_urls = {a.message_id: _msg_url(a, inbox.name) for a in referenced}

        # All inboxes this article is linked to. Used for both the
        # cross-post hint (which excludes the current inbox) and the
        # canonical URL (which picks one across the full set).
        all_links: list[tuple[int, str]] = list(session.execute(
            select(Inbox.id, Inbox.name)
            .join(ArticleList, ArticleList.inbox_id == Inbox.id)
            .where(ArticleList.article_id == article.id)
            .order_by(Inbox.name)
        ).all())
        cross_post_inboxes = [n for ix_id, n in all_links if ix_id != inbox.id]
        base = _site_base()
        canonical_url = _canonical_url_for(article, all_links, base=base)
        # Canonical inbox is what JSON-LD's isPartOf and the breadcrumb
        # should reflect, not necessarily the current URL's inbox.
        canonical_inbox_name = (
            _canonical_inbox_name(article, all_links) or inbox.name
        )
        page_json_ld = _json_ld_message(
            article, parsed, canonical_url or "", canonical_inbox_name, base,
        ) if canonical_url else None

        # Subsystem header + related patches + mainline applications +
        # patch-series timeline. Inlined into the route's main session
        # so a single connection covers the whole render, opening a
        # second SessionLocal here previously cost an extra connect /
        # WAL-snapshot acquire per message page.
        subsystem_hits = subsystems_for_article(session, article.id)
        touched_paths = list(session.execute(
            select(ArticleFile.path).where(ArticleFile.article_id == article.id)
        ).scalars())
        # `recent_patches_touching` is a `path IN (...)` join over
        # `article_files × articles` that gets reused across every
        # render of this article. Cache it; new ingest activity on
        # the same paths surfaces within the 5-minute TTL.
        related_patches = cache.get_or_compute(
            session,
            f"msg_related:{article.id}",
            300,
            lambda: recent_patches_touching(
                session, touched_paths,
                exclude_article_id=article.id, limit=5,
            ),
        ) if touched_paths else []
        mainline_applications = list(session.execute(
            select(MainlineCommit)
            .where(MainlineCommit.message_id == article.message_id)
            .order_by(MainlineCommit.committed_at.asc())
        ).scalars())
        patch_series_revisions: list[tuple[Article, str]] = []
        if article.patch_series_key:
            revisions = list(session.execute(
                select(Article)
                .options(selectinload(Article.lists))
                .where(Article.patch_series_key == article.patch_series_key)
                .order_by(Article.date.asc().nulls_last())
            ).scalars())
            for rev in revisions:
                link_set = [(al.inbox_id, al.inbox.name) for al in rev.lists]
                url = _canonical_url_for(rev, link_set, base="") or ""
                patch_series_revisions.append((rev, url))

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

    # Long threads switch to a right-rail tree layout on wide
    # viewports, see LONG_THREAD_SIDEBAR_THRESHOLD. Narrow
    # viewports stack regardless via the CSS media query.
    long_thread = len(thread) >= LONG_THREAD_SIDEBAR_THRESHOLD

    # HTMX intra-thread swap: when the click came from a tree link, return
    # only the message-body partial (just the <article id="msg">). The
    # surrounding tree + nav stay put on the client; the client-side script
    # flips which <li> carries the .is-active class after the swap.
    template = (
        "_message_body.html"
        if request.headers.get("HX-Request") == "true"
        else "message.html"
    )
    return render_template(
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
        parent_off_list=parent_off_list,
        parent_off_list_hints=parent_off_list_hints,
        related=related,
        cross_post_inboxes=cross_post_inboxes,
        canonical_url=canonical_url,
        page_json_ld=page_json_ld,
        subsystem_hits=subsystem_hits,
        related_patches=related_patches,
        mainline_applications=mainline_applications,
        patch_series_revisions=patch_series_revisions,
        long_thread=long_thread,
    )
