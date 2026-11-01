"""The whole-thread view: every message in one conversation on one page.

Additive to the message page, which keeps its own reading model (tree
stacked above a single body). This surface exists because a single
message is a poor unit for an index: most replies are a couple of lines
("Acked-by", "applied, thanks"), so a 60-message thread is 60 thin,
near-duplicate URLs. The conversation as a whole is the substantial,
genuinely unique document, so it is what the sitemap lists and what the
individual message pages canonicalise to.

The view PAGINATES at `settings.thread_view_render_cap` messages per
page and truncates nothing: every message is rendered on exactly one
page, and every page is self-canonical, sitemapped, and reachable by
following the next-link. A message's canonical names the page that
holds it (`threading.thread_page_of`), which is why the render order
and the rank predicate are one rule.

The exception is a thread the materialised `thread_root_id` column
cannot rank, which is served whole from the recursive walk and makes NO
page claims at all: its messages keep their own canonical and its root
is advertised as a message URL. See `threading.unmaterialised_roots`,
the single predicate every one of those surfaces consults.
"""

import hashlib
import logging
from collections import Counter, defaultdict

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
from mimir.canonical import fallback_canonical_name
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.lifecycle_status import lifecycle_status_for_articles
from mimir.models import (
    Article,
    ArticleList,
    Inbox,
)
from mimir.parser import normalize_subject
from mimir.patch_revisions import subject_tags
from mimir.patch_series import slot_rank
from mimir.patch_state import message_kind, patch_state_for_article, patch_timelines
from mimir.seo import _json_ld_thread
from mimir.store import read_messages
from mimir.subsystems import article_paths, is_cover, subsystems_for_paths
from mimir.subsystems_dashboard import most_active_subsystems_in_inbox
from mimir.threading import (
    dedupe_thread,
    find_thread_root,
    get_thread,
    outline_depths,
    thread_aggregates,
    thread_by_root_id,
    thread_is_materialised,
    thread_page_of,
    thread_parents,
    thread_rows,
    thread_series_rows,
    thread_sort_key,
    unmaterialised_roots,
)
from mimir.web._blueprint import bp_web
from mimir.web.filters import _clean_subject_filter, thread_summary_from
from mimir.web.routes._validators import render_state_tag, series_rows_tag
from mimir.web.urls import (
    MD_SUFFIX,
    _abort_404_if_url_date_mismatches,
    _get_inbox_or_404,
    _markdown_response,
    _msg_url,
    _site_base,
    markdown_path,
    thread_page_url,
)

logger = logging.getLogger(__name__)


# The outline lists this many messages of the thread (the first, in the
# page's order), and only for a thread of at least this many (#989).
OUTLINE_ROWS = 200
OUTLINE_MIN_MESSAGES = 5


def _message_href(root, inbox_name, page, article_id) -> str:
    """The thread page that holds a message, and its anchor, for the cards'
    "replied to", the outline and the Patches index. The permalink is
    `page_url` plus its id in the template: the same `thread_page_url`, for
    the page being rendered. Always the whole URL, never a bare `#m{id}`: an
    HTMX slice is appended to page 1's document without a URL change, so a
    bare fragment in it resolves to page 1, which does not hold the message
    (a copied permalink or a new-tab click lands on the wrong page)."""
    return thread_page_url(root.id, root.date, inbox_name, page) + f"#m{article_id}"


def _revision_hrefs(session, entries, *, cap, current, ordered_rows) -> dict[int, str]:
    """`article id -> thread page and anchor` for the revisions the page's
    folds link (the root's and each patch's), so a reader on `/t` stays in
    thread views. The page is the revision's index in its thread's rows
    (`index // cap + 1`, the Patches index's rank), in its canonical inbox,
    like the message page's canonical. One statement for the revisions, two
    per inbox for `unmaterialised_roots` (batched), and one `thread_rows`
    per OTHER thread (usually one per version), never one per revision;
    `current` (`(inbox_id, root_id)`) reuses `ordered_rows`.

    A revision whose thread is not materialised, or not ranked, gets no
    entry and keeps its message-page link. Another thread's ranks are not
    in this page's validator: a revision's page there moves only when an
    earlier-dated message joins ahead of it (the cap is in the ETag), and
    reading those threads on every 304 is not worth that rare stale link.
    """
    ids = {e.article_id for e in entries if not e.is_current} | {
        r.article_id for e in entries for r in e.reposts if not r.is_current
    }
    if not ids:
        return {}
    # Each revision's links with their roots, in one statement. The inbox
    # pick is `fallback_canonical_name`, the rule `_canonical_inbox_name`
    # applies for the revision's message-page URL.
    links: dict[int, list] = defaultdict(list)
    canonical_id: dict[int, int | None] = {}
    for rev_id, root_id, canon, box in session.execute(
        select(
            ArticleList.article_id,
            ArticleList.thread_root_id,
            Article.canonical_inbox_id,
            Inbox,
        )
        .join(Article, Article.id == ArticleList.article_id)
        .join(Inbox, Inbox.id == ArticleList.inbox_id)
        .where(ArticleList.article_id.in_(ids))
    ):
        links[rev_id].append((box, root_id))
        canonical_id[rev_id] = canon
    by_thread: dict[tuple, list[int]] = defaultdict(list)
    inboxes = {}
    for rev_id, rev_links in links.items():
        name = fallback_canonical_name(
            canonical_id[rev_id], [(box.id, box.name) for box, _ in rev_links]
        )
        box, root_id = next(
            (link for link in rev_links if link[0].name == name), (None, None)
        )
        if box is None or root_id is None:
            continue
        inboxes[box.id] = box
        by_thread[(box.id, root_id)].append(rev_id)
    others: dict[int, list[int]] = defaultdict(list)
    for key in by_thread:
        if key != current:
            others[key[0]].append(key[1])
    unranked = {
        (inbox_id, root_id)
        for inbox_id, roots in others.items()
        for root_id in unmaterialised_roots(session, inbox_id, roots)
    }
    hrefs = {}
    for (inbox_id, root_id), rev_ids in by_thread.items():
        if (inbox_id, root_id) == current:
            rows = ordered_rows
        elif (inbox_id, root_id) in unranked:
            continue
        else:
            rows = thread_rows(session, inboxes[inbox_id], root_id)
        index = {row.id: i for i, row in enumerate(rows)}
        root = next((row for row in rows if row.id == root_id), None)
        if root is None or root.date is None:
            continue
        for rev_id in rev_ids:
            if rev_id in index:
                hrefs[rev_id] = _message_href(
                    root, inboxes[inbox_id].name, index[rev_id] // cap + 1, rev_id
                )
    return hrefs


def _reply_markers(
    session, inbox, root, nodes, *, page, cap, ordered_rows, all_nodes
) -> dict:
    """`node id -> (parent's author, href)` for each message on this page
    whose parent is a message of the thread, for the cards' "replied to"
    (#989). The href is the thread page that holds the parent plus its
    anchor (`_message_href`, by the Patches index's rank, `index // cap + 1`).
    Nothing for a root, a self-parent or a parent outside the archive.

    No traversal, so a `thread_parent` cycle is just two markers. The
    parents on other pages cost one statement on the indexed path, none
    when they are all here, and none on the CTE fallback, which holds the
    whole thread."""
    on_page = {n.message_id: (n.id, n.author) for n in nodes}
    wanted = {n.thread_parent for n in nodes if n.thread_parent}
    known = dict(on_page)
    missing = [m for m in wanted if m not in known]
    if missing and all_nodes is not None:
        known.update({n.message_id: (n.id, n.author) for n in all_nodes})
    elif missing:
        for pid, mid, author in thread_parents(session, inbox, root.id, missing):
            known[mid] = (pid, author)
    page_of = {
        row.id: i // cap + 1
        for i, row in enumerate(ordered_rows if all_nodes is None else all_nodes)
    }
    markers = {}
    for n in nodes:
        parent = known.get(n.thread_parent)
        if parent is None or n.thread_parent == n.message_id:
            continue
        pid, author = parent
        parent_page = page if n.thread_parent in on_page else page_of.get(pid)
        if parent_page is None:
            continue
        markers[n.id] = (author, _message_href(root, inbox.name, parent_page, pid))
    return markers


def _outline_rows(nodes, *, root, kinds, ordered, cap, page, inbox_name, total_count):
    """The outline's rows (#989), from the thread's first messages: the root,
    the patches as one row, each reply indented by its depth among the rows
    listed, and a last row for what is not listed. Each links the page and
    anchor that hold its message (`_message_href`, by the Patches index's
    rank, `index // cap + 1`). `ordered` is
    the whole thread in page order; `kinds` is empty on the CTE fallback, which
    then lists every message but the root as a reply."""
    page_of = {row.id: i // cap + 1 for i, row in enumerate(ordered)}
    here = {n.id for n in nodes if page_of.get(n.id, 1) == page}

    def href(article_id):
        return _message_href(root, inbox_name, page_of.get(article_id, 1), article_id)

    patches = [n for n in nodes if n.id != root.id and kinds.get(n.id) == "patch"]
    replies = [n for n in nodes if n.id != root.id and kinds.get(n.id) != "patch"]
    depth = outline_depths(replies)
    rows = [{"kind": "root", "depth": 0, "node": nodes[0]}]
    if patches:
        rows.append({"kind": "patches", "depth": 0, "node": patches[0]})
    rows += [{"kind": "reply", "depth": depth[n.id], "node": n} for n in replies]
    for row in rows:
        row["href"] = href(row["node"].id)
        row["here"] = row["node"].id in here
    # Guarded on the rows read, not on `total_count`: that is read before
    # the validator and `ordered` after it, with no snapshot between, so a
    # reindex can leave fewer rows than the count says, and an ingest more,
    # which would make a "0 more" row. Both must hold.
    if total_count > len(nodes) and len(ordered) > len(nodes):
        rows.append(
            {
                "kind": "more",
                "depth": 0,
                "node": ordered[len(nodes)],
                "href": href(ordered[len(nodes)].id),
                "here": False,
                "remaining": total_count - len(nodes),
            }
        )
    return rows


@bp_web.route("/<inbox_name>/<int:year>/<int:month>/<int:article_id>/t" + MD_SUFFIX)
@bp_web.route(
    "/<inbox_name>/<int:year>/<int:month>/<int:article_id>/t/<int:page>" + MD_SUFFIX
)
def thread_view_md(
    inbox_name: str, year: int, month: int, article_id: int, page: int | None = None
):
    """One thread page as Markdown (#884), through the same view: the
    same pages, redirects, canonical and validator as the HTML."""
    return thread_view(inbox_name, year, month, article_id, page, markdown=True)


@bp_web.route("/<inbox_name>/<int:year>/<int:month>/<int:article_id>/t")
@bp_web.route("/<inbox_name>/<int:year>/<int:month>/<int:article_id>/t/<int:page>")
def thread_view(
    inbox_name: str,
    year: int,
    month: int,
    article_id: int,
    page: int | None = None,
    markdown: bool = False,
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
        # Built with `thread_page_url`. Every emitter of a thread-page
        # URL now resolves through `urls.thread_page_url` (and through
        # `_msg_path` beneath it), so the redirect target, this page's
        # canonical, the message page's canonical, the sitemap `<loc>`
        # and IndexNow are byte-identical by construction.
        #
        # "By construction" was an overstatement when first written and
        # is only now accurate. The PREFIX was shared from the start,
        # but the `/N` page suffix had three implementations: this
        # helper, `message.py`'s canonical, and IndexNow's, each
        # appending its own f-string. They agreed solely because all
        # three wrote the integer bare, which is the same coincidence
        # the sitemap's year/month spelling relied on one commit
        # earlier. Pinned by
        # `test_every_thread_page_emitter_agrees_byte_for_byte`, which
        # holds all four against the string the sitemap published.
        #
        # This was NOT true when first written: the sitemap hand-built
        # the same path with its own f-string, and the comment claimed
        # the sharing anyway. The route accepts both `2024/01` and
        # `2024/1`, so the two could have drifted apart with every test
        # still green. `test_sitemap_loc_is_byte_identical_to_the_pages_own_canonical`
        # now holds them together.
        if explicit_page and page == 1:
            target = thread_page_url(article.id, article.date, inbox.name)
            return redirect(markdown_path(target) if markdown else target, code=301)

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
                pg = 1
                if thread_is_materialised(session, inbox.id, root.id):
                    pg = thread_page_of(
                        session,
                        inbox.id,
                        root.id,
                        article,
                        settings.thread_view_render_cap,
                    )
                target = thread_page_url(root.id, root.date, inbox.name, pg)
                return redirect(markdown_path(target) if markdown else target, code=301)
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

        # At least 1: `Settings` refuses a smaller cap at startup (#612),
        # since 0 would render no root and 500 every thread view.
        cap = settings.thread_view_render_cap
        offset = (page - 1) * cap

        # The thread's series rows, for the validator (#989). The whole
        # thread's rows, which the tabs and the Patches index need, are
        # read only on a 200, below: on the largest thread they cost
        # 22.8 ms against 2.5 ms for these. Indexed path only, like
        # everything that names a page.
        hx_request = request.headers.get("HX-Request") == "true"
        series_rows: list = []
        all_nodes = None  # set on the CTE fallback only
        # The outline's source rows: the thread's first `OUTLINE_ROWS`, for
        # the full page of a thread of `OUTLINE_MIN_MESSAGES` or more. Read
        # before the validator, which digests them.
        outline_nodes: list = []
        if thread_rooted:
            # Paged: the slice of MESSAGES is fetched, the totals are
            # asked for separately, and no message body or ThreadNode is
            # built for the rest of the thread ON THIS PATH. The whole
            # thread's ROWS are read once, below, as columns only
            # (`thread_rows`, #989). Scoped deliberately:
            # `lifecycle_status_for_articles` below still runs a
            # recursive `WITH RECURSIVE descendants` over the whole
            # thread (scoped to this inbox), so the request as a whole
            # does still walk it. A 5-minute cache keyed by the ETag's
            # content part (shared by every page of the thread)
            # amortises that. Do not "simplify" the slice by fetching
            # every message and slicing in Python.
            series_rows = thread_series_rows(session, inbox, article.id)
            agg = thread_aggregates(session, inbox, article.id)
            total_count = agg.total
            thread_max_date = agg.last_activity or article.date
            authors = agg.authors
            want_outline = (
                not (markdown or hx_request) and total_count >= OUTLINE_MIN_MESSAGES
            )
            # Page 1 reads the outline's rows and slices its own out of
            # them, so the outline costs no statement there; later pages
            # read the outline's rows on their own.
            nodes = thread_by_root_id(
                session,
                inbox,
                article.id,
                offset=offset,
                limit=max(cap, OUTLINE_ROWS) if want_outline and page == 1 else cap,
            )
            rendered_nodes = nodes[:cap]
            if want_outline:
                outline_nodes = (
                    nodes[:OUTLINE_ROWS]
                    if page == 1
                    else thread_by_root_id(
                        session, inbox, article.id, limit=OUTLINE_ROWS
                    )
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
            if not (markdown or hx_request) and total_count >= OUTLINE_MIN_MESSAGES:
                outline_nodes = all_nodes[:OUTLINE_ROWS]

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
        # them changes the body (Markdown is a third representation,
        # #884, on its own URL). `hooks.py` also sends
        # `Vary: HX-Request` for this endpoint, which it did NOT until
        # a review caught these comments asserting it. The ETags
        # differ per representation, so revalidating caches were safe,
        # but bfcache and prerender do not revalidate.
        # Everything the page renders ABOUT the root that no message
        # carries: landing state, review roll-up, subsystem
        # attribution. None of them moves `total_count` or
        # `thread_max_date`, so without this a patch landing (or a
        # MAINTAINERS reparse, every 10 minutes in prod) leaves the
        # edge and every crawler holding a validator that pins the
        # exact "did $series land" text this release made canonical.
        state_tag = render_state_tag(session, article.id, root_msgid)
        # The header cards list this inbox's own subsystem first, which is
        # its busiest of the last 7 days: a cached ranking, read here and
        # never computed (`compute_on_miss=False`, the request-path
        # posture). It moves with activity, not with this thread, so its
        # top id is an ETag input; a cold cache is the empty tag, and the
        # ETag moves when it warms.
        busiest = most_active_subsystems_in_inbox(
            session, inbox, days=7, limit=1, compute_on_miss=False
        )
        own_subsystem_id = busiest[0].id if busiest else None
        # Split so the ETag is the content part plus the representation
        # (which page, full or htmx). The root's pill and card depend on
        # the content part alone, so their caches key by it: one entry
        # and one whole-thread walk per state, shared by every page of
        # the thread, instead of one per page variant.
        content_input = (
            f"thread|{article.id}|{mimir.__version__}|{cap}|{membership_source}|"
            f"{total_count}|"
            f"{thread_max_date.isoformat() if thread_max_date else ''}|{state_tag}|"
            f"{series_rows_tag(series_rows)}|{own_subsystem_id or ''}"
        )
        content_key = hashlib.blake2s(content_input.encode(), digest_size=8).hexdigest()
        representation = "md" if markdown else "hx" if hx_request else "full"
        # What the cards say about each reply's parent, for the messages
        # of THIS page only, so it is page-local and costs no statement:
        # the slice is read above. Not in `content_input`, which keys the
        # pill caches shared by every page of the thread.
        parents_tag = hashlib.blake2s(
            "|".join(
                f"{n.id}:{n.thread_parent or ''}" for n in rendered_nodes
            ).encode(),
            digest_size=8,
        ).hexdigest()
        # The outline's rows are the thread's first `OUTLINE_ROWS`, on every
        # page, and their parents set the depths it shows. Page 1 reads
        # them with its own slice; the digest is empty for the slice and
        # the Markdown page, which render no outline.
        outline_tag = hashlib.blake2s(
            "|".join(f"{n.id}:{n.thread_parent or ''}" for n in outline_nodes).encode(),
            digest_size=8,
        ).hexdigest()
        etag_input = (
            f"{content_input}|{page}|{representation}|{parents_tag}|{outline_tag}"
        )
        etag = hashlib.blake2s(etag_input.encode(), digest_size=8).hexdigest()
        if etag in request.if_none_match:
            response = Response(status=304)
            response.set_etag(etag)
            return response
        # A 200 from here on: now the whole thread's rows (#989), for the
        # kinds the full page and the HTMX slice render. The Markdown page
        # renders neither.
        ordered_rows = (
            thread_rows(session, inbox, article.id)
            if thread_rooted and not markdown
            else []
        )

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

        # The cards' parent markers, and which cards keep their subject
        # heading (only where it differs from the thread's, #989). Compared
        # as `subject_normalized` is, so `AW:`, `SV:` and forwards count as
        # replies here too (#1034). Not for the Markdown page, which renders
        # neither.
        reply_to: dict = {}
        heading_ids: set = set()
        if not markdown:
            root_key = normalize_subject(article.subject)
            heading_ids = {
                n.id for n in rendered_nodes if normalize_subject(n.subject) != root_key
            }
            reply_to = _reply_markers(
                session,
                inbox,
                article,
                rendered_nodes,
                page=page,
                cap=cap,
                ordered_rows=ordered_rows,
                all_nodes=all_nodes,
            )

        # One read for both the subsystem line and the series totals.
        # A cover's files are its patches' (#844), so for a series this
        # is the posting, and the validator digests the same rows.
        root_paths = article_paths(session, root_node)
        subsystem_hits = subsystems_for_paths(
            session, [row[2] for row in root_paths if row[2]]
        )
        # This inbox's own subsystem first (stable: the rest keep the
        # matcher's order), so the Subsystems and Maintainers cards lead
        # with what the list is about.
        subsystem_hits = sorted(subsystem_hits, key=lambda h: h.id != own_subsystem_id)
        subsystem_names = [h.name for h in subsystem_hits]
        # Only for a cover: a single patch carries git's diffstat in its
        # own body, so a total there would copy lore. The patches' rows
        # only: a cover can carry an interdiff, whose lines would count
        # twice (#939).
        series_totals = None
        if is_cover(root_node):
            # One copy per position, the slot's patch (#861): a resend is
            # the same posting, and its lines would count twice.
            ranks: dict[int, tuple[bool, int]] = {}
            for aid, pos, path, _, _ in root_paths:
                if pos:
                    rank = slot_rank(aid, path is not None)
                    ranks[pos] = min(ranks.get(pos, rank), rank)
            slots = {aid for _, aid in ranks.values()}
            files = [
                (path, ins, dels)
                for aid, _, path, ins, dels in root_paths
                if aid in slots and path
            ]
            # Line counts only once every file has one: a posting the
            # backfill has not reached in full would show a part as the
            # total.
            counted = bool(files) and all(ins is not None for _, ins, _ in files)
            series_totals = (
                len(ranks),
                len({path for path, _, _ in files}),
                sum(ins for _, ins, _ in files) if counted else None,
                sum(dels for _, _, dels in files) if counted else None,
            )

        # The root's patch surfaces. Every message page in the thread
        # canonicalises here (#1003), and the message page is otherwise
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
                state_key=content_key,
            )
            # Keyed by the ETag's content part, as on the message page: a
            # cached pill is reused only while that part is unchanged.
            root_lifecycle = lifecycle_status_for_articles(
                session, [root_node.id], inbox.id, state_key=content_key
            ).get(root_node.id)

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
        canonical_url = base + thread_page_url(
            root_node.id, root_node.date, inbox.name, page
        )
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
            next_page_url = thread_page_url(
                root_node.id, root_node.date, inbox.name, page + 1
            )
        if page > 1:
            prev_page_url = thread_page_url(
                root_node.id, root_node.date, inbox.name, page - 1
            )
        remaining = max(0, total_count - (offset + len(rendered_nodes)))

        # Tabs (#989): display only, so every message is still rendered
        # here. Counted as the issue counts them ("Discussion 76 | Patches
        # 27 | Everything 104" for 1 cover, 27 patches, 76 replies):
        # replies, patches, everything. A cover, the root's or another
        # posting's, is in neither of the first two; accepted in review. No
        # rows on the CTE fallback, so no kinds and no tabs there.
        kinds = {row.id: message_kind(row) for row in ordered_rows}
        kind_counts = Counter(kinds.values())
        show_tabs = total_count > 1 and kind_counts["patch"] > 0
        outline = (
            _outline_rows(
                outline_nodes,
                root=article,
                kinds=kinds,
                ordered=ordered_rows if all_nodes is None else all_nodes,
                cap=cap,
                page=page,
                inbox_name=inbox.name,
                total_count=total_count,
            )
            if outline_nodes
            else []
        )
        # The Patches index covers the whole thread. Each row links the
        # page holding the patch: its index in the page-ordered rows,
        # which `thread_page_of` agrees with (pinned by a test), through
        # the one thread-page URL builder.
        # Rendered by `thread.html` only: not for the HTMX slice, which
        # would otherwise re-read the whole series on every "load more".
        patch_index = []
        patch_revisions: dict = {}
        if show_tabs and representation == "full":
            patch_index = [
                {
                    "id": row.id,
                    "subject": row.subject,
                    "href": _message_href(root_node, inbox.name, i // cap + 1, row.id),
                }
                for i, row in enumerate(ordered_rows)
                if kinds[row.id] == "patch"
            ]
            # Revisions inline only for the root's own series when the root
            # is a cover: exactly the rows `render_state_tag` digests, so
            # the timelines need no validator input of their own (D5).
            # `patch_timelines` reads that series only, so a patch of
            # another series in the thread gets no entry.
            root_row = ordered_rows[0]
            if kinds[root_row.id] == "cover":
                patch_revisions = patch_timelines(
                    session,
                    root_row.patch_series_key,
                    root_row.patch_series_head_id,
                    [p["id"] for p in patch_index],
                    inbox.name,
                )
        revision_hrefs: dict[int, str] = {}
        if representation == "full":
            entries = [
                *(root_patch_state.series if root_patch_state else []),
                *(e for timeline in patch_revisions.values() for e in timeline),
            ]
            revision_hrefs = _revision_hrefs(
                session,
                entries,
                cap=cap,
                current=(inbox.id, root_node.id) if ordered_rows else None,
                ordered_rows=ordered_rows,
            )

    author_count = thread_summary_from(authors, thread_max_date, total_count)[
        "author_count"
    ]
    # The header's prefix tags and stripped title, from the parsers' own
    # bracket (`patch_bracket`); the <title> and structured data keep the
    # whole subject.
    title_tags, title_text = subject_tags(_clean_subject_filter(root_node.subject))
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
        author_count=author_count,
        last_activity=thread_max_date,
        title_tags=title_tags,
        title_text=title_text,
        kinds=kinds,
        outline=outline,
        reply_to=reply_to,
        heading_ids=heading_ids,
        kind_counts=kind_counts,
        show_tabs=show_tabs,
        patch_index=patch_index,
        patch_revisions=patch_revisions,
        revision_hrefs=revision_hrefs,
        canonical_url=canonical_url,
        page_json_ld=page_json_ld,
        root_url=_msg_url(root_node, inbox.name),
        subsystem_hits=subsystem_hits,
        series_totals=series_totals,
        patch_state=root_patch_state,
        lifecycle_status=root_lifecycle,
        page_url=thread_page_url(root_node.id, root_node.date, inbox.name, page),
        markdown_url=markdown_path(
            thread_page_url(root_node.id, root_node.date, inbox.name, page)
        ),
    )
    if markdown:
        return _markdown_response(
            render_template("thread.md", **context), etag, canonical_url
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
