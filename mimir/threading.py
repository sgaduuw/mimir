"""Thread reconstruction for the message view.

Uses SQLite recursive CTEs against the `thread_parent` graph (best-guess
parent: In-Reply-To OR last entry of References, computed at ingest):
- `find_thread_root` walks up to the topmost ancestor that's actually in
  our DB. A message whose parent isn't in the archive is its own root
  (we don't show phantom containers in v1).
- `get_thread` walks down from a root, building a `sort_path` column so
  the result is a proper depth-first traversal with siblings ordered
  by date.
- `thread_by_root_id` answers the same question as `get_thread` from
  the materialised `article_lists.thread_root_id` instead, in
  chronological order and without any traversal. It is what the
  whole-thread view uses; see its docstring for the measured
  difference (6 ms against 14.4 s on the largest production thread)
  and for why depth-first ordering is not preserved.
- `thread_window` gives the message page the part of a long thread
  around one message (path, nearest siblings, first replies) without
  walking the thread.

A 1000-deep depth limit guards against pathological cycles in the
underlying data (real lkml threads rarely exceed ~50 deep).
"""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import exists, func, literal, or_, select, text, tuple_
from sqlalchemy.orm import Session, aliased

from mimir import cache
from mimir.datetime_utils import coerce_dt
from mimir.models import Article, ArticleList, Inbox

MAX_DEPTH = 1000

# Stand-in for a NULL `articles.date` in ordering and rank comparisons.
# SQLite sorts NULL first, so the sentinel has to be below every real
# date; the corpus starts in the 1990s. Used identically by
# `thread_by_root_id`'s ORDER BY, `thread_page_of`'s rank count and
# `thread_sort_key`, because a disagreement between any two of them
# puts a message on a page its own canonical does not name.
# NAIVE deliberately, and `noqa`'d rather than "fixed": SQLite returns
# `Article.date` naive (verified 2026-09-27), so an aware sentinel would
# raise `TypeError: can't compare offset-naive and offset-aware
# datetimes` the first time `thread_sort_key` met a dateless row. Ruff
# offers the tzinfo as an UNSAFE fix; taking it would break the thread
# view's ordering silently.
_DATE_FLOOR = datetime(1, 1, 1)  # noqa: DTZ001
ACTIVE_THREADS_CACHE_TTL_SEC = 300  # 5 minutes


@dataclass
class ThreadNode:
    id: int
    message_id: str
    thread_parent: str | None
    subject: str | None
    author: str | None
    date: datetime | None
    # `None` on the `thread_by_root_id` path, which has no traversal to
    # derive depth from. Deliberately not 0: a plausible-looking zero
    # would be silently wrong for every reply, where None raises.
    depth: int | None


@dataclass
class ActiveThread:
    """A thread that's seen activity inside the recent-window. `id`,
    `inbox_name`, `message_id`, and `date` are the root article's, so
    `|msg_url` (which expects an Article-shape) works on this directly.

    `recent_count` counts every message in the window (including the root
    if it was sent during the window). `reply_count` is the same minus
    the root, so a brand-new thread posted today with no responses yet
    shows reply_count=0, recent_count=1.
    """

    id: int
    inbox_name: str
    message_id: str
    subject: str | None
    author: str | None
    date: datetime | None
    recent_count: int
    reply_count: int
    last_activity: datetime | None


cache.register("ActiveThread", ActiveThread)


def dedupe_thread(nodes: list) -> list:
    """Drop repeated articles from a `get_thread` walk, preserving order.

    A cyclic `thread_parent` (sender-controlled `In-Reply-To`, no cycle
    guard at ingest) makes the recursive CTE re-emit the same article
    once per level up to MAX_DEPTH. The thread view and the message
    page must count the SAME thread: the view renders the deduped list,
    so a message page counting the raw one would report 1001 messages
    where the view shows 1.
    """
    seen: set[int] = set()
    out = []
    for node in nodes:
        if node.id not in seen:
            seen.add(node.id)
            out.append(node)
    return out


def find_thread_root(session: Session, inbox: Inbox, message_id: str) -> str | None:
    """Return the message_id of the topmost ancestor present in this inbox.

    Reads the materialised `article_lists.thread_root_id` when it is
    populated, and falls back to the recursive walk while it is NULL.
    That fallback is what lets the column ship without a blocking
    backfill: a partially-filled corpus is correct, just not yet fast.

    The root's own membership is re-checked, so this keeps the
    contract in the first line: the topmost ancestor PRESENT IN THIS
    INBOX. A stored root can stop being a member without the FK's
    `SET NULL` firing, because `reindex --from-scratch` drops
    `article_lists` rows while the article survives. Without the
    re-check the fast path would answer with a root the walk would
    never return, and the caller would render an empty thread.

    NOT used by `thread_roots.verify_thread_roots`, which deliberately
    calls `_find_thread_root_cte` instead. Verification exists to catch
    a wrong column value, and a verifier that reads the column it is
    checking would agree, by construction, with any corruption whose
    stored root is still a member of this inbox. (A root that is NOT a
    member fails `find_thread_root`'s membership re-check and falls
    back to the CTE, so that narrow class would be caught either way.
    The reachable class is the first one.) Pinned by
    `test_verify_thread_roots_recomputes_rather_than_reading_the_column`.
    """
    row = session.execute(
        text(
            """
            SELECT root.message_id
              FROM article_lists al
              JOIN articles a ON a.id = al.article_id
              JOIN articles root ON root.id = al.thread_root_id
              JOIN article_lists rl
                ON rl.article_id = root.id AND rl.inbox_id = al.inbox_id
             WHERE al.inbox_id = :inbox_id AND a.message_id = :mid
            """
        ),
        {"inbox_id": inbox.id, "mid": message_id},
    ).scalar()
    if row is not None:
        return row
    return _find_thread_root_cte(session, inbox, message_id)


def _find_thread_root_cte(
    session: Session, inbox: Inbox, message_id: str
) -> str | None:
    """The recursive walk-up, ignoring the materialised column.

    Kept as the independent source of truth: it is both the fallback
    for unfilled rows and the oracle `verify_thread_roots` recomputes
    against.
    """
    sql = text(
        """
        WITH RECURSIVE ancestors AS (
            SELECT a.message_id, a.thread_parent, 0 AS depth
            FROM articles a
            JOIN article_lists al ON al.article_id = a.id
            WHERE al.inbox_id = :inbox_id AND a.message_id = :mid
            UNION ALL
            SELECT a.message_id, a.thread_parent, anc.depth + 1
            FROM articles a
            JOIN article_lists al ON al.article_id = a.id
            JOIN ancestors anc ON a.message_id = anc.thread_parent
            WHERE al.inbox_id = :inbox_id AND anc.depth < :max_depth
        )
        SELECT message_id FROM ancestors ORDER BY depth DESC LIMIT 1
        """
    )
    return session.execute(
        sql, {"inbox_id": inbox.id, "mid": message_id, "max_depth": MAX_DEPTH}
    ).scalar()


@dataclass
class ThreadAggregates:
    """Thread-wide values a single page cannot answer for itself.

    Once the view is paginated the rendered slice stops being the
    thread, so anything describing the whole conversation (its size, its
    newest activity, how many people are in it) has to be asked
    separately. Kept together because they come from one predicate and
    should not drift: the count feeds both the ETag and the visible
    total, and those disagreeing is how a page serves a stale body.

    `authors` is the DISTINCT author strings, not a count.
    `web.filters` derives the tally by `parseaddr`ing each one so
    display-name drift does not fragment it, which a SQL
    `COUNT(DISTINCT author)` would get wrong: `Alice <a@x>` and
    `alice <a@x>` are two strings and one person. Distinct-ing in SQL
    first is safe and keeps the result tiny (5 rows on the 12,342
    message syzbot thread).
    """

    total: int
    last_activity: datetime | None
    authors: list[str]


def thread_aggregates(
    session: Session, inbox: Inbox, root_article_id: int
) -> ThreadAggregates:
    """Whole-thread totals, without fetching the whole thread.

    Two indexed queries against `ix_article_lists_thread_root`, both
    measured at 6 ms on the largest production thread (2026-08-02).
    """
    total, last_activity = session.execute(
        select(func.count(), func.max(Article.date))
        .join(ArticleList, ArticleList.article_id == Article.id)
        .where(
            ArticleList.inbox_id == inbox.id,
            ArticleList.thread_root_id == root_article_id,
        )
    ).one()
    authors = list(
        session.execute(
            select(Article.author)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                ArticleList.thread_root_id == root_article_id,
                Article.author.is_not(None),
            )
            .distinct()
        ).scalars()
    )
    return ThreadAggregates(
        total=total or 0,
        last_activity=coerce_dt(last_activity),
        authors=authors,
    )


def unmaterialised_roots(
    session: Session, inbox_id: int, root_ids: list[int]
) -> set[int]:
    """Of `root_ids`, those whose thread has a NULL row the column
    cannot see.

    Read the summary literally: this detects MISSING values, not WRONG
    ones. A root whose stored `thread_root_id` is non-NULL and
    incorrect passes here, and every consumer treats passing as "this
    thread's page claims are safe". That is deliberate (CONTEXT.md: a
    plausible-but-wrong non-NULL value is strictly worse than none, and
    `find_incoherent_roots` is the detector for it) and it is not
    reachable from ingest, because the `dup_db` branch queues no
    `ArticleList` row so resolution only ever runs against a freshly
    inserted NULL row. But nothing at the call sites says coherence is
    someone else's job, so it is said here.

    One predicate, consulted by everything that renders a thread page or
    makes a claim about one. A thread it returns is rendered from the
    recursive walk and makes NO page claims anywhere: its messages
    self-canonicalise, the sitemap lists its root's message URL, and
    IndexNow announces message URLs. Less consolidated while the data is
    unrepaired, never pointing at a page that does not hold the message.

    TWO conditions, and both are necessary. An earlier version checked
    only the second, with a docstring asserting the first was "already
    excluded by the caller" -- true of one of its three callers, and the
    sentence is what let the hole survive re-reading.

    1. **The root must be its own root here.** `thread_root_id` is
       per-inbox and a root points at itself, so anything else (NULL, or
       another article) means the column has no answer for this thread.
       This is also what makes (2) sound: (2) finds unrooted rows by
       looking one hop up at a ROOTED parent, so with nothing rooted it
       finds nothing and would report a wholly-unrooted thread healthy.
       Reachable: `reindex --from-scratch` NULLs an entire inbox by
       design, `break_cycle` documents a stall that leaves rows NULL,
       and `drive_passes` can exhaust its budget.

    2. **No unrooted row may hang off the rooted set.** An unrooted
       member is invisible to every query keyed on the column, so the
       page renders it (via the walk) while the rank and the count do
       not see it, and every message after it drifts a page.

       Depth-1 is complete, and the derivation is (1) plus chain
       structure rather than anything about the shape ingest produces.
       If root R passes (1) it is self-rooted, and every member of R's
       thread reaches R by a chain of in-inbox parents; so on any chain
       from an unrooted member up to R there is a LOWEST rooted
       ancestor, which carries R, and its child is unrooted and one hop
       below it. That is a depth-1 hit. Where no chain reaches a rooted
       ancestor at all, R itself is unrooted and (1) fires.

       An earlier version argued this from
       `_pending._set_subtree_root(..., None)` nulling a contiguous
       DESCENDANT set, concluding that the topmost unrooted node has a
       rooted parent. That step is false for the region it names: that
       call fires only where the parent is present AND unrooted, so the
       unrooted region extends up THROUGH the article to its unrooted
       parent, and its topmost node has an unrooted parent. The
       conclusion held; the reason did not.

       Settled by enumeration rather than argument, which is the only
       reason the bad derivation was caught:
       `test_unmaterialised_roots_depth1_is_complete_over_every_shape`
       covers all 125 acyclic 4-node parent assignments x all 16 NULL
       patterns, and a sibling test samples 1,200 more varying
       per-inbox membership. Both assert the batched and single-root
       forms agree.

    Batched because the sitemap asks about thousands of roots at once.
    Driven from the unrooted side, whose cardinality is the amount of
    unrepaired data rather than the size of the inbox.
    """
    if not root_ids:
        return set()

    # (1) roots that are not self-rooted here, INCLUDING ones with no row
    # at all in this inbox.
    self_rooted = {
        art_id
        for art_id, root in session.execute(
            select(ArticleList.article_id, ArticleList.thread_root_id).where(
                ArticleList.inbox_id == inbox_id,
                ArticleList.article_id.in_(root_ids),
            )
        ).all()
        if root == art_id
    }
    bad = set(root_ids) - self_rooted

    # (2) roots with an unrooted row hanging off their rooted set.
    unrooted = aliased(ArticleList)
    unrooted_art = aliased(Article)
    parent_art = aliased(Article)
    parent_link = aliased(ArticleList)
    bad |= set(
        session.execute(
            select(parent_link.thread_root_id)
            .select_from(unrooted)
            .join(unrooted_art, unrooted_art.id == unrooted.article_id)
            .join(parent_art, parent_art.message_id == unrooted_art.thread_parent)
            .join(
                parent_link,
                (parent_link.article_id == parent_art.id)
                & (parent_link.inbox_id == unrooted.inbox_id),
            )
            .where(
                unrooted.inbox_id == inbox_id,
                unrooted.thread_root_id.is_(None),
                parent_link.thread_root_id.in_(root_ids),
            )
            .distinct()
        ).scalars()
    )
    return bad


def thread_is_materialised(
    session: Session, inbox_id: int, root_article_id: int | None
) -> bool:
    """Can the column answer for this one thread? See
    `unmaterialised_roots`, which this is the single-root form of.

    `None` is not materialised: a caller with no root to ask about has
    nothing the column can rank.
    """
    if root_article_id is None:
        return False
    return root_article_id not in unmaterialised_roots(
        session, inbox_id, [root_article_id]
    )


def thread_sort_key(root_article_id: int):
    """The thread view's ordering, as a Python sort key.

    Exists so the CTE fallback can page in the SAME order the indexed
    path does. It previously sliced `get_thread`'s depth-first output
    while `thread_page_of` computed a message's page chronologically,
    so on any non-linear thread the canonical named a page that did not
    contain the message. Two orderings, one claim.

    Mirrors `thread_by_root_id`'s ORDER BY, including that SQLite sorts
    NULL dates FIRST: `date is not None` is False for them, and False
    sorts before True.

    Uses `_DATE_FLOOR`, the same sentinel the SQL side COALESCEs to. It
    previously used `datetime.min`, which has the same VALUE and not the
    same meaning, and a comment three lines up claimed the sentinel was
    shared. One place to change if the floor ever moves.

    The two orderings are not identical in one unreachable corner: a row
    whose real date EQUALS the floor is interleaved with NULL-dated rows
    by SQLite (they COALESCE to the same value) while Python sorts all
    NULLs strictly first. Not producible from git commit times, and it
    is why the floor is year 1 rather than something plausible like
    1970.
    """

    def key(node):
        return (
            node.id != root_article_id,
            node.date is not None,
            node.date or _DATE_FLOOR,
            node.id,
        )

    return key


def thread_page_of(
    session: Session,
    inbox_id: int,
    root_article_id: int,
    article: Article,
    cap: int,
) -> int:
    """1-based page of the thread view that contains this message.

    Exists so the thread view and the message page cannot disagree
    about where a message lives. The message page needs it to build a
    canonical, the thread view needs it to build page URLs, and the two
    deriving it independently is precisely the emitter/acceptor split
    CONTEXT.md documents as this project's recurring defect: the
    canonical would point at a page that does not contain the message,
    silently, on the surface crawlers are aimed at.

    It counts rather than re-sorting in Python deliberately. The
    ordering lives in `thread_by_root_id`'s ORDER BY, and reproducing
    it as a Python sort key would be a second implementation of the
    same rule, including the NULL-date and tie-break behaviour. One
    indexed COUNT against the same predicate cannot drift from it.
    """
    if article.id == root_article_id:
        return 1
    # Rank among non-root messages, under the same `(date, id)` order
    # the page uses. `+ 1` for the root, which always occupies index 0.
    earlier = session.execute(
        select(func.count())
        .select_from(ArticleList)
        .join(Article, Article.id == ArticleList.article_id)
        .where(
            ArticleList.inbox_id == inbox_id,
            ArticleList.thread_root_id == root_article_id,
            Article.id != root_article_id,
            # COALESCE so a dateless row participates in the comparison
            # instead of evaluating to NULL and being skipped. Without
            # it the rank undercounted by the number of dateless
            # replies, which are sorted FIRST by the ORDER BY, so the
            # claimed page drifted below the real one.
            tuple_(
                func.coalesce(Article.date, _DATE_FLOOR),
                Article.id,
            )
            < tuple_(
                func.coalesce(literal(article.date), _DATE_FLOOR),
                literal(article.id),
            ),
        )
    ).scalar_one()
    return (earlier + 1) // max(1, cap) + 1


def thread_by_root_id(
    session: Session,
    inbox: Inbox,
    root_article_id: int,
    *,
    offset: int = 0,
    limit: int | None = None,
) -> list[ThreadNode]:
    """Every message of one thread, chronologically, root first.

    Same question `get_thread` answers, by a different route: it reads
    the materialised `article_lists.thread_root_id` instead of walking
    `thread_parent`. Measured on production 2026-08-02 against the
    largest thread in the corpus (syzbot, 12,342 messages, depth 63):
    6 ms here against 14,424 ms for the recursive walk, which was 93% of
    that page's response time and was paid even on a 304. Re-measured
    2026-10-30 on production, the whole thread with its `ThreadNode`s
    built: 41.4 ms (median of 5, warm cache), so the 6 ms is stale; the
    page reads one slice of it, never the whole thread.

    Two differences from `get_thread`, both deliberate:

    * **Chronological, not depth-first.** Reconstructing depth-first
      order needs the traversal this function exists to avoid, and
      nothing renders the hierarchy: `ThreadNode.depth` reaches
      `thread.html` and is used zero times there. The whole-thread page
      is a flat view, and arrival order is what a flat view of a
      mailbox means.
    * **`depth` is None, not 0.** There is no depth without the walk,
      and a plausible-looking 0 would be silently wrong for every reply.
      `None` makes any future arithmetic on it raise instead.

    The root sorts first regardless of its date. `articles.date` is the
    public-inbox commit time, so a reply ingested before its parent
    carries an earlier timestamp, and the JSON-LD builder requires the
    root at index 0. `id` breaks date ties so paging is deterministic.

    It also has no depth cap, where `get_thread` stops at `MAX_DEPTH`
    and so silently returns a SHORT thread beyond 1000 levels. That is
    latent rather than live (production's deepest thread is 63, measured
    2026-08-02) and it was found by accident while benchmarking, but do
    not "restore consistency" by adding a cap here: that would reinstate
    silent data loss. Pinned by
    `test_thread_by_root_id_is_not_capped_by_max_depth`.

    Callers must confirm the article IS its own materialised root before
    using this. A row whose `thread_root_id` is NULL (not yet computed)
    or points elsewhere (a non-settling cycle) is not answerable here
    and belongs on `get_thread`.
    """
    rows = session.execute(
        select(
            Article.id,
            Article.message_id,
            Article.thread_parent,
            Article.subject,
            Article.author,
            Article.date,
        )
        .join(ArticleList, ArticleList.article_id == Article.id)
        .where(
            ArticleList.inbox_id == inbox.id,
            ArticleList.thread_root_id == root_article_id,
        )
        .order_by(*_thread_order(root_article_id))
        .offset(offset or None)
        .limit(limit)
    ).all()
    return [_node(r, None) for r in rows]


def _thread_order(root_article_id: int) -> tuple:
    """The thread page's order: root first, then `(date, id)`. One
    expression for every reader that must agree with the page, so a
    message's index in any of them is its index on the page."""
    return (
        (Article.id != root_article_id),
        # Same COALESCE as `thread_page_of`'s rank predicate. The
        # two must agree on where dateless rows sit or a message
        # canonicalises to a page that does not hold it.
        #
        # `Article.id` here is DEFENSIVE and deliberately untested:
        # deleting it does not change behaviour on SQLite, because
        # rows with equal dates already arrive in rowid order, which
        # is the same order this imposes. So no fixture can
        # distinguish it and mutation-testing reports it as a
        # survivor. It stays because "equal keys happen to come back
        # in id order" is a property of the current plan, not a
        # guarantee, and the rank predicate above (which IS
        # observable, and tested) sorts by it explicitly. The two
        # disagreeing is the failure mode.
        func.coalesce(Article.date, _DATE_FLOOR),
        Article.id,
    )


def _thread_select(inbox: Inbox, root_article_id: int, *columns):
    """`columns` of the messages of one thread in one inbox: the join and
    the two filters every thread-page reader shares."""
    return (
        select(*columns)
        .join(ArticleList, ArticleList.article_id == Article.id)
        .where(
            ArticleList.inbox_id == inbox.id,
            ArticleList.thread_root_id == root_article_id,
        )
    )


def thread_series_rows(session: Session, inbox: Inbox, root_article_id: int) -> list:
    """The thread's rows that carry a series column, for the thread page's
    validator (#989): their key, head, position and version decide each
    message's kind, and a reprocess can rewrite them without adding a
    message. Filtered in SQL, so a 304 does not read the whole thread:
    2.5 ms against 22.8 ms for `thread_rows` on the 12,342-message syzbot
    thread, which has no such rows (production, 2026-10-30, median of 5).
    A row with no series column has a kind that follows from its subject,
    which never changes."""
    return session.execute(
        _thread_select(
            inbox,
            root_article_id,
            Article.id,
            Article.patch_series_key,
            Article.patch_series_head_id,
            Article.patch_series_position,
            Article.patch_series_version,
        )
        .where(
            or_(
                Article.patch_series_key.is_not(None),
                Article.patch_series_position.is_not(None),
            )
        )
        .order_by(Article.id)
    ).all()


def thread_rows(session: Session, inbox: Inbox, root_article_id: int) -> list:
    """Every message of one thread in the page's order, with the series
    columns `message_kind` reads (#989): the tabs, the Patches index and
    the page of each patch (`index // cap + 1`, which equals
    `thread_page_of`). Only after the validator, on a 200: 22.8 ms for
    the 12,342-message syzbot thread on production (2026-10-30, median of
    5, warm cache), too much for every 304.

    Same precondition as `thread_by_root_id`: the article is its own
    materialised root."""
    return session.execute(
        _thread_select(
            inbox,
            root_article_id,
            Article.id,
            Article.subject,
            Article.date,
            Article.patch_series_key,
            Article.patch_series_head_id,
            Article.patch_series_position,
            Article.patch_series_version,
        ).order_by(*_thread_order(root_article_id))
    ).all()


def outline_depths(nodes) -> dict[int, int]:
    """`node id -> depth` among `nodes` only, for the thread page's outline
    (#989): the number of listed ancestors, so a reply whose parent is not
    listed (a patch, the cover, beyond the outline's cap, outside the
    archive) is at depth 0. Iterative and linear, and a `thread_parent`
    cycle or a self-parent ends the walk at the first repeat, so it gives a
    finite depth: `dedupe_thread` exists because cycles happen."""
    parent = {n.message_id: n.thread_parent for n in nodes}
    depth: dict[str, int] = {}
    for start in parent:
        chain, cur, seen = [], start, set()
        while cur in parent and cur not in depth and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = parent[cur]
        base = depth[cur] if cur in depth else -1
        for message_id in reversed(chain):
            base += 1
            depth[message_id] = base
    return {n.id: depth[n.message_id] for n in nodes}


def thread_parents(
    session: Session, inbox: Inbox, root_article_id: int, message_ids: list[str]
) -> list:
    """`(id, message_id, author)` of those messages of one thread, for the
    thread page's "replied to" markers (#989): the parents that sit on
    another page than their reply. One statement, scoped to the thread so a
    Message-ID cannot name a message of another one. Same precondition as
    `thread_by_root_id`."""
    return session.execute(
        _thread_select(
            inbox, root_article_id, Article.id, Article.message_id, Article.author
        ).where(Article.message_id.in_(message_ids))
    ).all()


# The message page's window onto a thread longer than one thread-view
# page (#798). The path limit matches the CSS depth ladder, which stops
# indenting at 20.
WINDOW_PATH_MAX = 20
WINDOW_SIBLINGS = 5
WINDOW_CHILDREN = 10


@dataclass
class ThreadGap:
    """Messages the window leaves out, rendered as one row.

    A path gap counts the ancestors it stands for, exactly. A sibling or
    reply gap counts direct replies only: their own replies are not
    counted, so the page calls those "replies" rather than "messages".
    """

    depth: int
    gap_count: int
    replies: bool = True


@dataclass
class ThreadWindow:
    """What the message page renders of a long thread.

    `rows` is a depth-first subsequence of the thread (ThreadNode and
    ThreadGap), with true depths. `hidden_replies` maps a shown node's
    id to how many of its replies are neither shown nor counted by a
    gap. `reply_count` is the article's direct replies.
    """

    rows: list
    hidden_replies: dict[int, int]
    reply_count: int


def _node(row, depth: int | None) -> ThreadNode:
    return ThreadNode(
        id=row.id,
        message_id=row.message_id,
        thread_parent=row.thread_parent,
        subject=row.subject,
        author=row.author,
        date=coerce_dt(row.date),
        depth=depth,
    )


def thread_window(
    session: Session, inbox: Inbox, root: Article, article: Article
) -> ThreadWindow | None:
    """The part of a long thread around `article`, without walking it.

    The path from the root, up to WINDOW_SIBLINGS replies to the same
    parent on each side, and the article's first WINDOW_CHILDREN
    replies. SQLite reads rows in proportion to the article's depth and
    to the number of direct replies of the shown messages (a cover
    letter with 2,077 replies reads 2,077 index entries to pick 10),
    never the whole thread; Python receives only the window. The walk
    down from the root (`get_thread`) was 12,342 rows and 4.9 MB of
    HTML on a message page in production's largest thread (2026-10-20).

    Siblings are ordered by `(COALESCE(date, _DATE_FLOOR), id)`, the
    order `thread_page_of` ranks by. Scoped to the inbox with EXISTS on
    the `article_lists` primary key rather than a JOIN, which without
    `sqlite_stat1` is driven from `ix_article_lists_inbox_id` (see
    `lifecycle_status._BULK_SQL`).

    Returns None when the walk up does not reach `root`, which means the
    column and the parent chain disagree. The caller renders the whole
    thread then, as it does for any thread the column cannot answer.
    """
    in_inbox = exists().where(
        ArticleList.article_id == Article.id, ArticleList.inbox_id == inbox.id
    )
    # Stops at the root rather than at MAX_DEPTH. `thread_parent` comes
    # from sender-controlled headers, so the chain can be a cycle, and a
    # walk without this stop circles it a thousand times.
    path = session.execute(
        text(
            """
            WITH RECURSIVE up AS (
                SELECT a.id, a.message_id, a.thread_parent, a.subject,
                       a.author, a.date, 0 AS depth
                  FROM articles a
                 WHERE a.id = :article_id
                UNION ALL
                SELECT a.id, a.message_id, a.thread_parent, a.subject,
                       a.author, a.date, up.depth + 1
                  FROM up
                  JOIN articles a ON a.message_id = up.thread_parent
                 WHERE up.id != :root_id AND up.depth < :max_depth
                   AND EXISTS (SELECT 1 FROM article_lists al
                                WHERE al.article_id = a.id
                                  AND al.inbox_id = :inbox_id)
            )
            SELECT id, message_id, thread_parent, subject, author, date
              FROM up ORDER BY depth DESC
            """
        ),
        {
            "article_id": article.id,
            "root_id": root.id,
            "inbox_id": inbox.id,
            "max_depth": MAX_DEPTH,
        },
    ).all()
    if not path or path[0].id != root.id:
        return None
    ancestors = [_node(r, d) for d, r in enumerate(path[:-1])]
    depth = len(ancestors)
    path_ids = [r.id for r in path]
    key = (func.coalesce(Article.date, _DATE_FLOOR), Article.id)
    here = tuple_(
        func.coalesce(literal(article.date), _DATE_FLOOR), literal(article.id)
    )

    def replies_to(message_id: str):
        # Path members are excluded so a cycle cannot show one twice.
        return select(
            Article.id,
            Article.message_id,
            Article.thread_parent,
            Article.subject,
            Article.author,
            Article.date,
        ).where(
            Article.thread_parent == message_id,
            in_inbox,
            Article.id.not_in(path_ids),
        )

    def count_replies(message_id: str, *where):
        return session.execute(
            select(func.count()).select_from(
                replies_to(message_id).where(*where).subquery()
            )
        ).scalar_one()

    rows: list = []
    accounted: Counter = Counter()
    if len(ancestors) <= WINDOW_PATH_MAX:
        rows.extend(ancestors)
    else:
        keep = WINDOW_PATH_MAX - 1
        rows.append(ancestors[0])
        rows.append(
            ThreadGap(
                depth=1, gap_count=len(ancestors) - WINDOW_PATH_MAX, replies=False
            )
        )
        rows.extend(ancestors[-keep:])
        # The root's reply on the path is behind the gap.
        accounted[root.message_id] += 1

    before: list[ThreadNode] = []
    after: list[ThreadNode] = []
    n_before = n_after = 0
    if ancestors:
        parent_mid = ancestors[-1].message_id
        siblings = replies_to(parent_mid)
        before = [
            _node(r, depth)
            for r in session.execute(
                siblings.where(tuple_(*key) < here)
                .order_by(key[0].desc(), key[1].desc())
                .limit(WINDOW_SIBLINGS)
            ).all()
        ][::-1]
        after = [
            _node(r, depth)
            for r in session.execute(
                siblings.where(tuple_(*key) > here)
                .order_by(*key)
                .limit(WINDOW_SIBLINGS)
            ).all()
        ]
        n_before = count_replies(parent_mid, tuple_(*key) < here)
        n_after = count_replies(parent_mid, tuple_(*key) > here)
        accounted[parent_mid] += (n_before - len(before)) + (n_after - len(after))

    children = [
        _node(r, depth + 1)
        for r in session.execute(
            replies_to(article.message_id).order_by(*key).limit(WINDOW_CHILDREN)
        ).all()
    ]
    n_children = count_replies(article.message_id)
    accounted[article.message_id] += n_children - len(children)

    if n_before > len(before):
        rows.append(ThreadGap(depth=depth, gap_count=n_before - len(before)))
    rows.extend(before)
    rows.append(_node(article, depth))
    rows.extend(children)
    if n_children > len(children):
        rows.append(ThreadGap(depth=depth + 1, gap_count=n_children - len(children)))
    rows.extend(after)
    if n_after > len(after):
        rows.append(ThreadGap(depth=depth, gap_count=n_after - len(after)))

    shown = [r for r in rows if isinstance(r, ThreadNode)]
    accounted.update(n.thread_parent for n in shown)
    reply_counts = dict(
        session.execute(
            select(Article.thread_parent, func.count())
            .where(
                Article.thread_parent.in_([n.message_id for n in shown]),
                in_inbox,
            )
            .group_by(Article.thread_parent)
        ).all()
    )
    hidden = {
        n.id: reply_counts.get(n.message_id, 0) - accounted[n.message_id]
        for n in shown
        if reply_counts.get(n.message_id, 0) > accounted[n.message_id]
    }
    return ThreadWindow(rows=rows, hidden_replies=hidden, reply_count=n_children)


def get_thread(
    session: Session, inbox: Inbox, root_message_id: str
) -> list[ThreadNode]:
    """Return the full thread rooted at `root_message_id` within `inbox`,
    depth-first by date."""
    sql = text(
        """
        WITH RECURSIVE thread AS (
            SELECT a.id, a.message_id, a.thread_parent, a.subject, a.author, a.date,
                   0 AS depth,
                   CAST(a.date AS TEXT) || '|' || printf('%020d', a.id) AS sort_path
            FROM articles a
            JOIN article_lists al ON al.article_id = a.id
            WHERE al.inbox_id = :inbox_id AND a.message_id = :root
            UNION ALL
            SELECT a.id, a.message_id, a.thread_parent, a.subject, a.author, a.date,
                   t.depth + 1,
                   t.sort_path || '/' || CAST(a.date AS TEXT) || '|' || printf('%020d', a.id)
            FROM articles a
            JOIN article_lists al ON al.article_id = a.id
            JOIN thread t ON a.thread_parent = t.message_id
            WHERE al.inbox_id = :inbox_id AND t.depth < :max_depth
        )
        SELECT id, message_id, thread_parent, subject, author, date, depth
        FROM thread
        ORDER BY sort_path
        """
    )
    rows = session.execute(
        sql, {"inbox_id": inbox.id, "root": root_message_id, "max_depth": MAX_DEPTH}
    ).all()
    return [_node(r, r.depth) for r in rows]


_ORDER_CLAUSES = {
    # Half-life-decayed activity score: fresh bursts outrank old steady
    # chatter. Used for the 7-day "Most active threads" surface.
    "score": "score DESC, last_activity DESC",
    # Plain recency: most-recently-active thread first. Used for daily
    # views where decay is irrelevant (everything is within ~24h).
    "last_activity": "last_activity DESC, recent_count DESC",
}


def _active_threads_query(
    session: Session,
    inbox: Inbox,
    start: datetime,
    end: datetime,
    *,
    order_by: str = "score",
    limit: int | None = None,
    extra_ctes_sql: str = "",
    extra_seed_filter_sql: str = "",
    extra_params: dict | None = None,
) -> list[ActiveThread]:
    """Run the active-threads recursive CTE over a `[start, end)` window.

    `order_by`: 'score' (decay-weighted) or 'last_activity' (recency).
    `limit`: None means unbounded (return every thread with at least one
    message in the window).

    `extra_ctes_sql`: optional comma-suffixed CTE definitions injected
    after `WITH RECURSIVE` and before `chains`. The per-subsystem
    dashboard uses this to materialise the path-matched article ids
    once (via `AS MATERIALIZED`) instead of re-evaluating the
    `article_files` subquery for every seed row.

    `extra_seed_filter_sql`: optional SQL fragment AND-ed into the
    seed step's WHERE clause. Used together with `extra_ctes_sql`
    to reference the materialised CTE. Caller-supplied, must
    reference bind parameters only via the `extra_params` dict
    (not string-interpolated values).
    """
    order_sql = _ORDER_CLAUSES[order_by]
    limit_sql = f"LIMIT {int(limit)}" if limit is not None else ""
    sql = text(
        f"""
        WITH RECURSIVE {extra_ctes_sql} chains AS (
            SELECT a.id AS recent_id, a.message_id AS curr, a.thread_parent,
                   a.date AS recent_date, 0 AS depth
            FROM articles a
            JOIN article_lists al ON al.article_id = a.id
            WHERE al.inbox_id = :inbox_id AND a.date >= :start AND a.date < :end
              {extra_seed_filter_sql}
            UNION ALL
            SELECT c.recent_id, a.message_id, a.thread_parent,
                   c.recent_date, c.depth + 1
            FROM articles a
            JOIN article_lists al ON al.article_id = a.id
            JOIN chains c ON a.message_id = c.thread_parent
            WHERE al.inbox_id = :inbox_id AND c.depth < :max_depth
        ),
        max_depth AS (
            SELECT recent_id, MAX(depth) AS d FROM chains GROUP BY recent_id
        ),
        roots AS (
            SELECT c.recent_id, c.curr AS root_id, c.recent_date
            FROM chains c
            JOIN max_depth m ON m.recent_id = c.recent_id AND m.d = c.depth
        )
        SELECT a.id, a.message_id, a.subject, a.author, a.date AS root_date,
               COUNT(*) AS recent_count,
               SUM(CASE WHEN r.recent_id <> a.id THEN 1 ELSE 0 END) AS reply_count,
               MAX(r.recent_date) AS last_activity,
               -- Clamp at 0 days: pow(0.5, -N) blows up to huge values
               -- and lets a single future-dated row (typoed Date: 2099,
               -- mis-ingested commit_time, anything) dominate the
               -- ranking. articles.date is the public-inbox commit time
               -- per CONTEXT.md so future dates shouldn't arise on the
               -- SQL row, but the audit (2026-05-15) flagged the
               -- missing defensive clamp as a real silent-bug surface.
               SUM(pow(0.5, MAX(julianday('now') - julianday(r.recent_date), 0))) AS score
        FROM roots r JOIN articles a ON a.message_id = r.root_id
        GROUP BY r.root_id
        ORDER BY {order_sql}
        {limit_sql}
        """
    )
    params = {
        "inbox_id": inbox.id,
        "start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end": end.strftime("%Y-%m-%d %H:%M:%S"),
        "max_depth": MAX_DEPTH,
    }
    if extra_params:
        params.update(extra_params)
    rows = session.execute(sql, params).all()
    return [
        ActiveThread(
            id=r.id,
            inbox_name=inbox.name,
            message_id=r.message_id,
            subject=r.subject,
            author=r.author,
            date=coerce_dt(r.root_date),
            recent_count=r.recent_count,
            reply_count=r.reply_count,
            last_activity=coerce_dt(r.last_activity),
        )
        for r in rows
    ]


def active_threads(
    session: Session,
    inbox: Inbox,
    days: int = 7,
    limit: int = 10,
    force: bool = False,
) -> list[ActiveThread]:
    """Most active threads in `inbox` in the last `days` days.
    Cached on disk for ACTIVE_THREADS_CACHE_TTL_SEC (5 min) per
    (inbox, days, limit) key. Pass force=True to bypass and recompute."""

    def compute() -> list[ActiveThread]:
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        return _active_threads_query(
            session, inbox, start, end, order_by="score", limit=limit
        )

    return cache.get_or_compute(
        session,
        f"active_threads:{inbox.name}:{days}:{limit}",
        ACTIVE_THREADS_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def threads_for_day(
    session: Session,
    inbox: Inbox,
    day: date,
    force: bool = False,
) -> list[ActiveThread]:
    """Every thread in `inbox` with at least one message on `day`
    (UTC), ordered by last activity desc."""

    def compute() -> list[ActiveThread]:
        start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        end = start + timedelta(days=1)
        return _active_threads_query(
            session, inbox, start, end, order_by="last_activity", limit=None
        )

    return cache.get_or_compute(
        session,
        f"threads_for_day:{inbox.name}:{day.isoformat()}",
        ACTIVE_THREADS_CACHE_TTL_SEC,
        compute,
        force=force,
    )


# Cap on the "what I missed" window, keeps the recursive CTE
# bounded for a UI surface that anyone can hit. Operators returning
# from a holiday longer than this see "showing last 90 days" rather
# than a 30-second query.
THREADS_SINCE_MAX_DAYS = 90

# Cap on threads listed. lkml has ~38,000 threads in 90 days; listing
# them all means a lifecycle-pill cache write per id and an `IN :ids`
# past SQLite's variable limit. The list is newest activity first, so
# the cap keeps the most recently active threads.
THREADS_SINCE_MAX_THREADS = 200


def since_window_start(since: date, end: datetime) -> datetime:
    """Start of the "since" window: midnight UTC of `since`, clamped
    to `THREADS_SINCE_MAX_DAYS` before `end`. One rule for the thread
    list and the message total, so the two cover the same window."""
    start = datetime.combine(since, datetime.min.time(), tzinfo=UTC)
    return max(start, end - timedelta(days=THREADS_SINCE_MAX_DAYS))


def threads_since(
    session: Session,
    inbox: Inbox,
    since: date,
    force: bool = False,
) -> list[ActiveThread]:
    """Every thread in `inbox` with at least one message after
    `since` (UTC, inclusive of that whole day) up to now, ordered
    by last activity desc.

    Window is clamped to `THREADS_SINCE_MAX_DAYS` (90 days) below
    the present so a "since 2010" URL doesn't drag a multi-year
    CTE walk into a synchronous request. Caller renders a
    "showing last N days" notice when the requested window
    exceeds the cap; this helper returns whatever fits in the
    capped window.

    Returns at most `THREADS_SINCE_MAX_THREADS + 1` threads: the extra
    one tells the caller the list was cut, and the caller drops it.
    Callers pass the already-clamped date so every pre-floor request
    shares one cache key.
    """

    def compute() -> list[ActiveThread]:
        end = datetime.now(UTC)
        start = since_window_start(since, end)
        if start >= end:
            return []
        return _active_threads_query(
            session,
            inbox,
            start,
            end,
            order_by="last_activity",
            limit=THREADS_SINCE_MAX_THREADS + 1,
        )

    return cache.get_or_compute(
        session,
        f"threads_since:{inbox.name}:{since.isoformat()}",
        ACTIVE_THREADS_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def threads_for_month(
    session: Session,
    inbox: Inbox,
    year: int,
    month: int,
    limit: int = 100,
    force: bool = False,
) -> list[ActiveThread]:
    """Top-`limit` threads in `inbox` with at least one message in
    `(year, month)` (UTC), ordered by last activity desc. Caller is
    responsible for validating year/month bounds.

    A busy month on lkml has thousands of threads; rendering them all
    blows the response up past 1 MB. The view pairs this list with a
    `monthly_volume` count so the operator can see the total even when
    the rendered list is capped.
    """

    def compute() -> list[ActiveThread]:
        start = datetime(year, month, 1, tzinfo=UTC)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=UTC)
        else:
            end = datetime(year, month + 1, 1, tzinfo=UTC)
        return _active_threads_query(
            session, inbox, start, end, order_by="last_activity", limit=limit
        )

    return cache.get_or_compute(
        session,
        f"threads_for_month:{inbox.name}:{year:04d}-{month:02d}:{limit}",
        ACTIVE_THREADS_CACHE_TTL_SEC,
        compute,
        force=force,
    )
