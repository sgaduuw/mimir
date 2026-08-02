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

from flask import (
    Response,
    abort,
    make_response,
    redirect,
    render_template,
    request,
)
from sqlalchemy import select

import mimir
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import (
    Article,
    ArticleList,
)
from mimir.lifecycle_status import lifecycle_status_for_articles
from mimir.patch_state import patch_state_for_article
from mimir.seo import _json_ld_thread
from mimir.store import read_messages
from mimir.subsystems import subsystems_for_article
from mimir.threading import (
    dedupe_thread,
    find_thread_root,
    get_thread,
    thread_aggregates,
    thread_by_root_id,
    thread_is_materialised,
    thread_page_of,
    thread_sort_key,
)
from mimir.web._blueprint import bp_web
from mimir.web.filters import thread_summary_from
from mimir.web.routes._validators import render_state_tag
from mimir.web.urls import (
    _abort_404_if_url_date_mismatches,
    _get_inbox_or_404,
    _msg_url,
    _site_base,
    _thread_view_url,
)

logger = logging.getLogger(__name__)


@bp_web.route("/<inbox_name>/<int:year>/<int:month>/<int:article_id>/t")
@bp_web.route("/<inbox_name>/<int:year>/<int:month>/<int:article_id>/t/<int:page>")
def thread_view(
    inbox_name: str, year: int, month: int, article_id: int, page: int | None = None
):
    # `page` is attacker-supplied, and an astronomically large value
    # reaching SQLite as an OFFSET used to 500 the month sitemap on a
    # public URL. Same bound, same reason. Page 1 keeps the bare `/t`
    # so no URL that crawlers already hold moves.
    # `None` distinguishes the bare `/t` from an explicit `/t/1`. Both
    # render page 1; only the second is a redundant URL. Defaulting this
    # to 1 made the bare route redirect to itself.
    explicit_page = page is not None
    if page is None:
        page = 1
    # Bounds first, and before any redirect: an astronomically large page
    # reaching SQLite as an OFFSET 500'd the month sitemap once.
    if not 1 <= page <= 10_000:
        abort(404)

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

        # `/t/1` collapses to the bare `/t`, but ONLY once the inbox, the
        # article, its membership and the URL's date have been checked.
        # Redirecting first reflected raw URL segments into a `Location`
        # header: every bogus path suffixed `/t/1` became a cacheable 301
        # to a 404, and a segment containing an encoded CRLF raised
        # `ValueError: Header values must not contain newline characters`
        # inside `redirect()`, i.e. a 500 on a public URL. Cloudflare
        # forwards those bytes rather than rejecting them.
        #
        # Built with `_thread_view_url`, the same helper the sitemap, the
        # canonical and IndexNow use, so the target is byte-identical to
        # them by construction rather than by a hand-written f-string
        # that has to be kept in step.
        if explicit_page and page == 1:
            return redirect(_thread_view_url(article, inbox.name), code=301)

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
                # Land on the page that actually renders this reply, not
                # page 1. Dropping the page contradicted the reply's own
                # canonical, which names the page holding it, and it
                # laundered an out-of-range page into a 200. Guarded on
                # the same predicate everything else uses: an unrankable
                # thread has no page to name, so it keeps the bare `/t`.
                target = _thread_view_url(root, inbox.name)
                if thread_is_materialised(session, inbox.id, root.id):
                    pg = thread_page_of(
                        session,
                        inbox.id,
                        root.id,
                        article,
                        max(1, settings.thread_view_render_cap),
                    )
                    if pg > 1:
                        target += f"/{pg}"
                return redirect(target, code=301)
            root_msgid = article.message_id

        # Membership from the materialised column, NOT a walk. Measured
        # 2026-08-02 on the largest production thread (syzbot, 12,342
        # messages): 6 ms against 14,424 ms for `get_thread`, which was
        # 93% of that page's response and was paid even on a 304,
        # because the validator is derived from the thread's shape.
        #
        # `thread_is_materialised` owns the whole question of whether
        # the column may answer, including that this article is its own
        # root here. Asking half of it at the call site is how a earlier
        # version reported a wholly-unrooted thread healthy: every
        # message then claimed page 1 while the view paginated.
        #
        # By this point `root_msgid` is always `article.message_id`: the
        # block above either redirected or reset it, so the render root
        # is always `article` itself.
        thread_rooted = thread_is_materialised(session, inbox.id, article.id)
        # Named because it is an ETag input: the two paths order
        # messages identically, but they answer from different sources,
        # and a body that could differ must not share a validator with
        # one that does.
        membership_source = "idx" if thread_rooted else "cte"

        # A cap below 1 would render no messages and then IndexError in
        # the JSON-LD builder (which needs a root), 500ing every thread
        # view. Clamp rather than validate: this is an ops knob and an
        # unusable value should degrade, not take the surface down.
        cap = max(1, settings.thread_view_render_cap)
        offset = (page - 1) * cap

        if membership_source == "idx":
            # Paged: the slice is fetched, the totals are asked for
            # separately, and the whole thread is never materialised.
            # That is the point of the change, so do not "simplify" this
            # by fetching everything and slicing in Python.
            agg = thread_aggregates(session, inbox, article.id)
            total_count = agg.total
            thread_max_date = agg.last_activity or article.date
            authors = agg.authors
            rendered_nodes = thread_by_root_id(
                session, inbox, article.id, offset=offset, limit=cap
            )
        else:
            # `dedupe_thread` is only needed on the walk: a cyclic
            # `thread_parent` makes the CTE re-emit an article once per
            # level. The indexed query cannot repeat a row, because
            # `article_lists` is keyed `(article_id, inbox_id)` and the
            # query pins one inbox.
            all_nodes = dedupe_thread(get_thread(session, inbox, root_msgid))
            if not all_nodes:
                abort(404)
            total_count = len(all_nodes)
            thread_max_date = (
                max((n.date for n in all_nodes if n.date is not None), default=None)
                or article.date
            )
            authors = [n.author for n in all_nodes]
            # Sorted into the SAME order the indexed path uses before
            # slicing. Slicing `get_thread`'s depth-first output while
            # `thread_page_of` ranks chronologically put a message on a
            # page its own canonical did not name, on any thread that
            # is not a linear chain.
            # Visible in production, deliberately. This branch means
            # the column could not answer, which is a data-repair
            # condition and not a normal read: without a line here it is
            # indistinguishable from the fast path except by latency.
            logger.warning(
                "thread-view: %s/%s fell back to the recursive walk; "
                "thread has unrooted members and its pages are not "
                "advertised or canonicalised until the backfill repairs "
                "them",
                inbox.name,
                article.id,
            )
            all_nodes.sort(key=thread_sort_key(article.id))
            rendered_nodes = all_nodes[offset : offset + cap]

        if total_count == 0:
            abort(404)
        # A page past the end 404s rather than rendering an empty
        # conversation, for the same reason the month sitemap does: an
        # empty 200 is a URL a crawler keeps re-fetching.
        total_pages = max(1, -(-total_count // cap))
        if page > total_pages:
            abort(404)

        # An empty slice is a 404, not a 500. `total_count` and the slice
        # are separate queries and pysqlite gives a read session no
        # snapshot isolation across statements, so rows vanishing between
        # them (a concurrent `reindex --from-scratch`) left `page > 1`
        # passing the bounds check with nothing to render, and
        # `_json_ld_thread` indexes `nodes[0]`.
        if not rendered_nodes:
            abort(404)

        # The render root is always `article`: the redirect block above
        # either sent a reply to its root or reset `root_msgid` to this
        # article, so there is no case left where they differ. Taking it
        # from the node list would break on page 2, where the root is
        # not in the slice.
        root_node = article

        # ETag before any blob read, mirroring the message route: on a
        # 304 we skip the whole render, which matters more here than
        # anywhere else in the app (a page is up to
        # `thread_view_render_cap` blob fetches). Cap, page and the
        # HTMX-vs-full representation are all inputs because each of
        # them changes the body. `hooks.py` also sends
        # `Vary: HX-Request` for this endpoint, which it did NOT until
        # a review caught these comments asserting it. The ETags
        # differ per representation, so revalidating caches were safe,
        # but bfcache and prerender do not revalidate.
        hx_request = request.headers.get("HX-Request") == "true"
        # Everything the page renders ABOUT the root that no message
        # carries: landing state, review roll-up, subsystem
        # attribution. None of them moves `total_count` or
        # `thread_max_date`, so without this a patch landing (or a
        # MAINTAINERS reparse, every 10 minutes in prod) leaves the
        # edge and every crawler holding a validator that pins the
        # exact "did $series land" text this release made canonical.
        state_tag = render_state_tag(session, article.id, root_msgid)
        etag_input = (
            f"thread|{article.id}|{mimir.__version__}|{cap}|{page}|"
            f"{'hx' if hx_request else 'full'}|{membership_source}|{total_count}|"
            f"{thread_max_date.isoformat() if thread_max_date else ''}|{state_tag}"
        )
        etag = hashlib.blake2s(etag_input.encode(), digest_size=8).hexdigest()
        if etag in request.if_none_match:
            response = Response(status=304)
            response.set_etag(etag)
            return response

        # One bulk read rather than one `read_message` per node: that
        # reopened the epoch's dulwich Repo every time, 31-79% of this
        # page's blob cost depending on inbox (see `store.read_messages`
        # for the measurements). This is the page the sitemap aims
        # crawlers directly at, on a two-worker web tier.
        parsed_by_msgid = read_messages(
            session, inbox, [n.message_id for n in rendered_nodes]
        )

        # One message per entry: the parsed body for rendering, or None
        # when the blob is unreachable. A single missing blob (a mirror
        # gap, a re-packed epoch) must not 404 the whole conversation,
        # so the node keeps its header row and loses only its body.
        messages: list[tuple] = []
        for node in rendered_nodes:
            parsed = parsed_by_msgid.get(node.message_id)
            if parsed is None:
                logger.warning(
                    "thread-view: blob unavailable for %s in %s",
                    node.message_id,
                    inbox.name,
                )
            messages.append((node, parsed, _msg_url(node, inbox.name)))

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
                # `_days_since_last_reply` reduces this to the newest
                # date later than the article's own, so the whole-thread
                # maximum is the entire answer. Passing every date would
                # mean materialising the thread again, which is the cost
                # this route just stopped paying.
                thread_dates=[thread_max_date],
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
        # SELF-canonical, including the page suffix. Pointing page 2 at
        # page 1 would assert that they are the same document, which
        # they are not: Google's pagination guidance warns the content
        # of later pages may then go unindexed, and since messages on
        # page 2 canonicalise TO page 2, it would also build a canonical
        # CHAIN (message -> /t/2 -> /t). Caught by
        # `test_sitemap_lists_every_thread_page_and_each_one_resolves`,
        # which is what an advertised-but-self-disclaiming page looks
        # like from the outside.
        page_path = _thread_view_url(root_node, inbox.name)
        if page > 1:
            page_path += f"/{page}"
        canonical_url = base + page_path
        # Page 1 only. Pages 2+ do not contain the root's body, so a
        # `DiscussionForumPosting` rooted at a post the page does not
        # carry would misrepresent it. Structured data is optional; a
        # wrong one is not better than none.
        #
        # This also drops the `BreadcrumbList` from pages 2+, since it
        # rides in the same graph. Deliberate, not an oversight: those
        # pages are already linked, self-canonical and sitemapped, so
        # breadcrumbs would add a second emission path for a secondary
        # signal on a page whose place in the hierarchy is already
        # stated three other ways.
        page_json_ld = None
        if page == 1:
            page_json_ld = _json_ld_thread(
                nodes=rendered_nodes,
                parsed_by_id={n.id: p for n, p, _ in messages if p is not None},
                canonical_url=canonical_url,
                inbox_name=inbox.name,
                base=base,
                total_replies=total_count - 1,
                last_activity=thread_max_date,
                subsystem_names=subsystem_names,
            )

        next_page_url = None
        prev_page_url = None
        if page < total_pages:
            next_page_url = f"{_thread_view_url(root_node, inbox.name)}/{page + 1}"
        if page > 1:
            prev_page_url = _thread_view_url(root_node, inbox.name) + (
                "" if page == 2 else f"/{page - 1}"
            )
        remaining = max(0, total_count - (offset + len(rendered_nodes)))

    thread_summary = thread_summary_from(authors, thread_max_date, total_count)
    context = dict(
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        root=root_node,
        messages=messages,
        total_count=total_count,
        page=page,
        total_pages=total_pages,
        next_page_url=next_page_url,
        prev_page_url=prev_page_url,
        remaining=remaining,
        page_size=cap,
        thread_summary=thread_summary,
        canonical_url=canonical_url,
        page_json_ld=page_json_ld,
        root_url=_msg_url(root_node, inbox.name),
        subsystem_hits=subsystem_hits,
        patch_state=root_patch_state,
        lifecycle_status=root_lifecycle,
    )
    # Same URL, two representations, exactly as `message.py` does it:
    # HTMX gets just the next slice to append, everything else gets the
    # page. Both are folded into the ETag above, and `hooks.py` sends
    # `Vary: HX-Request` for this endpoint (it was added there for
    # `web.thread_view` only after a review found this comment claiming
    # a header nothing was setting).
    template = "_thread_messages.html" if hx_request else "thread.html"
    response = make_response(render_template(template, **context))
    response.set_etag(etag)
    return response
