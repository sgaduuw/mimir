"""Sitemap XML for crawlers.

Four cached surfaces feeding `/sitemap.xml`, `/meta-sitemap.xml`,
`/sitemap-maintainers.xml`, and `/<inbox>/sitemap.xml`. Cache keys are
stable; bumping `cache.NAMESPACE_VERSION` invalidates everything if a
payload shape changes, so per-route expiry is purely about freshness.

Each surface returns a `SitemapPayload` carrying both the XML body and
the most-recent `<lastmod>` date represented in that body. Route
handlers project the date into the HTTP `Last-Modified` header and
honour `If-Modified-Since` for conditional GETs, so crawlers (Google
in particular) can re-fetch the sitemap on a real change rather than
relying on body-content compare which they deprioritise.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from mimir import cache
from mimir.config import settings
from mimir.maintainer_directory import all_maintainers, maintainer_path
from mimir.models import Article, ArticleList, Inbox
from mimir.subsystems import is_addressable_subsystem_name, subsystem_path
from mimir.threading import unmaterialised_roots
from mimir.subsystems_dashboard import (
    MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP,
    most_active_subsystems_in_inbox,
)

SITEMAP_RECENT_PER_INBOX = 5000

# URLs per month sitemap page.
#
# TWO ceilings apply and the smaller one is not the obvious one:
#
#   1. sitemaps.org allows 50,000 URLs per urlset. This is the ceiling
#      everyone reaches for, and it is not the binding one.
#   2. SQLite's SQLITE_LIMIT_VARIABLE_NUMBER is 32,766 on this build
#      (measured 2026-07-30, sqlite 3.50.4). A page slice is passed
#      whole to three separate queries as an expanding `IN` list, so
#      the slice width IS a bound-parameter count.
#
# This was 45,000, which satisfies (1) and breaches (2). `git` 2016-06
# is the widest bucket on this corpus and raised `too many SQL
# variables`, returning 500 on every request while `/sitemap.xml`
# advertised it and nothing warmed it, leaving a crawler to find out.
#
# That bucket holds **89,679** roots, counted against production
# 2026-07-31 (rows in inbox `git` where `thread_root_id = article_id`
# and the root's own date falls in 2016-06, i.e. exactly what the month
# sitemap emits; confirmed by summing its five served pages). An earlier
# comment here said 40,429, understating it 2.2x. The conclusion is
# unaffected and gets stronger, not weaker: 89,679 breaches the bind
# limit far more decisively than 40,429 does. A historical month cannot
# grow, so that was a mis-measurement rather than drift, which is the
# reason it is worth correcting instead of refreshing.
#
# 20,000 sits under both with room to spare, and costs only that the
# one oversized bucket pages five times instead of once. Correctness
# per page is unchanged: a page is still a fixed-width slice of a
# snapshot, so it cannot exceed this number however fast the month
# grows. (An earlier comment claimed the margin absorbed growth between
# the index and the page being fetched, which was never true.)
#
# `test_sitemap_page_width_stays_under_the_sqlite_bind_limit` pins this
# against the runtime's ACTUAL limit rather than a copy of the number,
# because every paging test monkeypatches the width down to 2 and so
# cannot see the shipped value at all.
#
# NOT in the cache key. Changing it re-slices every page, so a deploy
# that changes it must bump `cache.NAMESPACE_VERSION` too, or pages
# cut under the old width serve for up to a TTL against an index
# counted under the new one.
#
# The 45,000 -> 20,000 change did NOT need that bump, and the reason is
# worth stating so nobody reads it as an oversight: month sitemaps have
# never been deployed, so no cache row anywhere was cut under the old
# width. There is no stale row for a bump to invalidate.
#
# Note the namespace itself is NOT unreleased: `NAMESPACE_VERSION` has
# been 3 since before v3.6.3, and production was holding ~15,000 warm
# rows under `v3:` when measured on 2026-07-30 (3,413
# `articles_reviewed_by`, 1,856 `active_threads_in_subsystem`, and so on
# down every subsystem-dashboard and lifecycle family). So bumping would
# not have been a harmless no-op, it would have cold-started all of that
# to fix nothing. Do not read "no bump here" as "this branch may change
# cached payload shapes freely"; the rule above still binds for every
# later change, including any further move of this width once month
# sitemaps have actually shipped.
#
# Paging is NOT speculative. Measured on production 2026-07-28: of
# 32,093 (inbox, month) buckets, 32,092 hold under 10k roots and ONE
# holds 40,429 (a floor; the true count is higher). That bucket is
# `git` 2016-06, which carries 288,315 messages against a next-busiest
# month of 8,035, because `articles.date` is the public-inbox COMMIT
# time and a wholesale archive import clusters into whatever month it
# was seeded. Every future `admin inbox add` can produce another, so
# this is recurrent by construction rather than one bad row.
SITEMAP_URLS_PER_PAGE = 20000
SITEMAP_TTL_SEC = 3600


@dataclass
class SitemapPayload:
    """The body + last-modified pair for one sitemap surface. The
    last_modified field is an ISO-8601 date string (`YYYY-MM-DD`) or
    None when the sitemap has no datable content. Routes parse it back
    into a datetime to populate `Last-Modified` on the HTTP response.
    The cache (`cache.register("SitemapPayload", ...)` below) stores
    the full dataclass so a warmed body + lastmod are always
    consistent."""

    body: str
    last_modified: str | None


cache.register("SitemapPayload", SitemapPayload)


def _build_sitemap_xml(entries: list[tuple[str, str | None]]) -> str:
    """Render an XML <urlset> sitemap. Each entry is
    `(loc, lastmod | None)`; when `lastmod` is None the element is
    omitted. Caller formats the timestamp, date-only `YYYY-MM-DD`
    is what Google's docs recommend for crawl-scheduling and is what
    mimir emits."""
    root = Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for loc, lastmod in entries:
        url_el = SubElement(root, "url")
        SubElement(url_el, "loc").text = loc
        if lastmod:
            SubElement(url_el, "lastmod").text = lastmod
    return '<?xml version="1.0" encoding="utf-8"?>\n' + tostring(
        root, encoding="unicode"
    )


def _build_sitemap_index_xml(entries: list[tuple[str, str | None]]) -> str:
    """Render a <sitemapindex> referencing sub-sitemaps. Same
    `(loc, lastmod)` shape as `_build_sitemap_xml`; the schema and
    element names differ, `<sitemapindex>` of `<sitemap>` rather
    than `<urlset>` of `<url>`."""
    root = Element("sitemapindex", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for loc, lastmod in entries:
        sm = SubElement(root, "sitemap")
        SubElement(sm, "loc").text = loc
        if lastmod:
            SubElement(sm, "lastmod").text = lastmod
    return '<?xml version="1.0" encoding="utf-8"?>\n' + tostring(
        root, encoding="unicode"
    )


def _per_inbox_latest_date(session) -> dict[str, str | None]:
    """One round-trip: max(article.date) per inbox, as `YYYY-MM-DD`
    strings (or None when the inbox is empty). Feeds the sitemap
    index `<lastmod>` per sub-sitemap."""
    rows = session.execute(
        select(Inbox.name, func.max(Article.date))
        .join(ArticleList, ArticleList.inbox_id == Inbox.id)
        .join(Article, Article.id == ArticleList.article_id)
        .where(Article.date.is_not(None))
        .group_by(Inbox.id)
    ).all()
    return {name: (dt.strftime("%Y-%m-%d") if dt else None) for name, dt in rows}


def _sitemap_entries_for_roots(
    roots: list[tuple[int, datetime]],
    singleton_ids: set[int],
    last_activity: dict[int, datetime],
    message_counts: dict[int, int],
    unrankable_roots: set[int],
    inbox_name: str,
    base: str,
) -> tuple[list[tuple[str, str | None]], str | None]:
    """`(loc, lastmod)` entries for thread roots, plus the newest lastmod.

    Shared by the flat per-inbox sitemap and the per-month ones, which
    differ only in how the root list was selected. Two copies of the
    `/t`-versus-message decision and the lastmod pick would be two
    places to drift, on a surface where drift is silent: the sitemap
    would list a URL whose own page names a different canonical.

    A singleton's lastmod is its own date by definition, said
    explicitly rather than left to the aggregate, which keeps this
    coherent with `_singleton_root_ids` BY CONSTRUCTION. That function
    reads `thread_parent` while the aggregate reads the materialised
    column, so a wrong column value could otherwise make a
    single-message page, which can never change, advertise another
    thread's date. For correct data the branch is a no-op.
    """
    cap = max(1, settings.thread_view_render_cap)
    entries: list[tuple[str, str | None]] = []
    newest: str | None = None
    for art_id, date in roots:
        loc = f"{base}/{inbox_name}/{date.year}/{date.month:02d}/{art_id}"
        activity = last_activity.get(art_id)
        count = message_counts.get(art_id, 1)
        # A thread the column cannot fully rank is advertised as its
        # root's MESSAGE url, exactly like a singleton, because that is
        # what its messages claim as their canonical in that state. The
        # thread view still exists and is still self-canonical; it is
        # simply not advertised while nothing can agree on how many
        # pages it has. Listing `/t` here instead would advertise a URL
        # that the messages themselves disclaim, which is the
        # incoherence `test_sitemap_is_coherent_midway_through_a_backfill`
        # exists to catch, and did.
        if art_id in singleton_ids or art_id in unrankable_roots:
            entries.append((loc, date.strftime("%Y-%m-%d")))
            stamp = date.strftime("%Y-%m-%d")
        else:
            loc += "/t"
            stamp = (activity or date).strftime("%Y-%m-%d")
            # EVERY page, not just the first. Since 3.8.0 the thread
            # view is paginated and each page is self-canonical, so a
            # page absent from the sitemap is reachable only by walking
            # the `next` chain, one hop deeper each time. Messages on it
            # canonicalise TO it, which would leave them deferring to a
            # URL crawlers never fetch.
            #
            # Measured against production 2026-08-02: at cap 75 a few
            # thousand of 6,074,810 threads paginate, adding entries in
            # the low tens of thousands against 6.07M advertised thread
            # URLs, i.e. well under 1%. An earlier version of this note
            # compared that figure to 32,229, which is the number of
            # sitemap FILES in the index, not URLs, and so overstated
            # the cost by about 188x. Pagination adds no index entries
            # at all. Re-derive both numbers, and say which population
            # they count, before deriving anything from them.
            #
            # The extra pages carry the same `<lastmod>` as page 1
            # because a reply anywhere changes the thread's newest
            # date, which is the freshness signal being reported.
            pages = max(1, -(-count // cap))
            for pg in range(1, pages + 1):
                entries.append((loc if pg == 1 else f"{loc}/{pg}", stamp))
        if newest is None or stamp > newest:
            newest = stamp
    return entries, newest


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """Half-open `[start, end)` for one month, in UTC.

    A range predicate rather than `strftime(...) = '2024-06'` so the
    date index stays usable: wrapping the column in a function makes it
    unsargable, which is the same trap CONTEXT.md records for
    `LIKE 'prefix%' ESCAPE` on `article_files.path`.
    """
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    )
    return start, end


def _month_roots_query(inbox: Inbox, year: int, month: int):
    """Thread roots in this inbox whose ROOT is dated in this month.

    Ordered deterministically, because pages are `LIMIT`/`OFFSET`
    slices of it and an unstable order would drop and duplicate URLs
    across page boundaries.
    """
    start, end = _month_bounds(year, month)
    return (
        select(Article.id, Article.date)
        .join(ArticleList, ArticleList.article_id == Article.id)
        .where(
            ArticleList.inbox_id == inbox.id,
            ArticleList.thread_root_id == Article.id,
            Article.date >= start,
            Article.date < end,
        )
        .order_by(Article.date.desc(), Article.id.desc())
    )


def _month_root_counts(session, inbox: Inbox) -> list[tuple[int, int, int]]:
    """`(year, month, root_count)` per month that has roots, oldest first.

    Drives the index: each bucket contributes
    `ceil(count / SITEMAP_URLS_PER_PAGE)` entries, so the index has to
    know the count before it can name the pages. sitemaps.org forbids
    an index referencing another index, so the pages cannot be hidden
    behind a nested index and must be enumerated here.

    Cost, measured on a full-size corpus (17.3M articles / 28.4M
    `article_lists` rows matching the production distribution):
    **7.7 s for the largest inbox, 35.7 s for the whole 203-inbox
    sweep**, min-of-3 and treat it as a floor, since the DB exceeds
    page cache.

    An earlier version of this docstring claimed 13.5 ms and ~3.4 s.
    Both were measured on a 10x-scaled-down corpus and then reported as
    production figures, and the pair was internally impossible before
    anyone re-measured: 13.5 ms for the largest inbox cannot coexist
    with 3.4 s for all 203, since the largest is a sixth of the rows.
    Arithmetic alone falsified it.

    Where the cost actually sits, which is the part worth knowing:

        >= 1M rows      5 inboxes    16.2 s
        100k-1M        55 inboxes    14.5 s
        10k-100k      102 inboxes     4.9 s
        < 10k          41 inboxes     0.2 s
        ------------------------------------
        non-huge      198 inboxes    19.6 s

    The 198 non-huge inboxes cost MORE in aggregate than the 5 huge
    ones, and the 55-inbox middle band alone carries 41% of the total.
    Quoting only the largest and smallest, as the earlier version did,
    skips the band that dominates. This is the standing "benchmark the
    worst-shaped instance, not the biggest" rule, and it caught this
    file twice before.

    The 1 h TTL means this is a once-hourly rebuild rather than
    per-request, but note the rebuild lands in a `warm-cache --tier
    fast` tick whose own docstring budgets "sub-100 ms each". A ~36 s
    tick once an hour is tolerable and worth knowing about; splitting
    the index into per-inbox pieces would fix both that and the entry
    ceiling below.

    Both this and `_recent_thread_roots_query` lead on `inbox_id`; do
    not assert WHICH index the planner picks, because it is
    corpus-dependent (a small corpus chooses
    `ix_article_lists_thread_root`, a production-sized one
    `ix_article_lists_inbox_id`) and the sibling docstring already says
    a two-column equality between columns of one row is not sargable.
    Cost is the same either way.

    The index runs to ~32k entries (~2.6 MiB), measured against prod:
    32,093 (inbox, month) buckets on 2026-07-28. That is inside the
    protocol's 50k-per-index and 50 MB limits, but 203 inboxes x 258
    months is already 52,374 POSSIBLE buckets, so the ceiling is nearer
    than a growth rate alone suggests: ~2,400 new entries a year, ~7
    years of headroom, and nothing in code enforces the cap. Recorded
    rather than solved; the fix is per-inbox sub-indexes.
    """
    y = func.strftime("%Y", Article.date).label("y")
    m = func.strftime("%m", Article.date).label("m")
    rows = session.execute(
        select(y, m, func.count())
        .join(ArticleList, ArticleList.article_id == Article.id)
        .where(
            ArticleList.inbox_id == inbox.id,
            ArticleList.thread_root_id == Article.id,
            Article.date.is_not(None),
        )
        .group_by(y, m)
        .order_by(y, m)
    ).all()
    return [(int(yy), int(mm), n) for yy, mm, n in rows]


def _recent_thread_roots_query(inbox: Inbox):
    """`(article_id, date)` for this inbox's thread roots, newest first.

    A root is `thread_root_id = article_id`: a plain per-row comparison
    between two columns of the same already-fetched row. Note what the
    planner actually does, because the obvious reading is wrong: the
    index seek is on `inbox_id` alone (EXPLAIN picks
    `ix_article_lists_inbox_id`), and the root test is then a filter
    over that narrowed set, NOT a seek against
    `ix_article_lists_thread_root`. A two-column equality between
    columns of the same row is not sargable.

    The win is therefore about what the predicate no longer does, not
    about a new index: it replaces a correlated EXISTS over
    `thread_parent` evaluated per candidate row, and with it the whole
    EXISTS-versus-JOIN tradeoff that cost two review rounds (the JOIN
    form was ~37x slower on the ~199 small inboxes, the EXISTS form
    ~8x slower on lkml). Measured on a 559k-row skewed corpus, new
    versus old: lkml-shaped inbox 87 ms versus 181 ms, small inbox
    0.154 ms versus 0.329 ms. Faster on both shapes, including the
    199-of-200 case that the earlier attempt regressed.

    Rows still awaiting the backfill carry NULL, which fails the
    comparison, so they are simply absent from the sitemap until the
    backfill reaches them rather than being listed wrongly. Freshness,
    not correctness.
    """
    return (
        select(Article.id, Article.date)
        .join(ArticleList, ArticleList.article_id == Article.id)
        .where(
            ArticleList.inbox_id == inbox.id,
            Article.date.is_not(None),
            ArticleList.thread_root_id == Article.id,
        )
        .order_by(Article.date.desc())
    )


def _thread_last_activity(
    session, inbox: Inbox, root_ids: list[int]
) -> dict[int, datetime]:
    """`root_id -> newest message date in that thread, in this inbox.`

    The `<lastmod>` for a thread URL has to be the date of the newest
    message IN it, not the root's own date. Those differ by the whole
    life of the conversation, and the difference is the entire point:
    a thread that gains a reply is a document that CHANGED, and the
    root's date never moves, so before this the consolidated page (the
    one this workstream exists to get indexed) announced nothing when
    it grew. Google does not consume IndexNow, so the sitemap is its
    only freshness channel for that event.

    This was previously left undone for a stated reason: deriving last
    activity "needs the batched recursive walk-up `active_threads`
    uses", measured in CONTEXT.md at ~700 ms for a 12k-message window.
    W8 removed that: thread membership is now a column, so this is one
    grouped aggregate over an indexed `(inbox_id, thread_root_id)`
    range rather than a recursive walk.

    Scoped to one inbox because threading is: the same root can head
    threads of different lengths, and therefore different last-activity
    dates, in each inbox it is cross-posted to.

    Measured on a 998k-row / 200-inbox corpus with realistic thread
    sizes: 9.4 ms for an lkml-shaped inbox at the 5000-root sitemap
    cap, 0.47 ms for a small inbox (553 roots). EXPLAIN uses
    `ix_article_lists_thread_root` on BOTH columns, unlike
    `_recent_thread_roots_query` which only seeks on `inbox_id`. The
    199 small inboxes are FASTER here rather than slower, because the
    cost is bounded by root count and not by inbox size, so the shape
    that dominates production (199-of-200 are small) is the cheap one.
    Whole warm cycle across 200 inboxes is ~100 ms.

    Mid-backfill a reply whose `thread_root_id` is still NULL is not
    counted, so the answer can be older than the truth. That direction
    is safe. `<lastmod>` can only delay a re-crawl, never pin one (the
    3.6.1 conditional-GET reasoning), so an understated date costs
    freshness until the backfill lands; an overstated one would waste
    crawl budget on documents that had not changed.
    """
    if not root_ids:
        return {}
    rows = session.execute(
        select(ArticleList.thread_root_id, func.max(Article.date))
        .join(Article, Article.id == ArticleList.article_id)
        .where(
            ArticleList.inbox_id == inbox.id,
            ArticleList.thread_root_id.in_(root_ids),
            Article.date.is_not(None),
        )
        .group_by(ArticleList.thread_root_id)
    ).all()
    # No `is not None` filter: the WHERE already excludes NULL dates,
    # so a group cannot aggregate to NULL.
    return dict(rows)


def _thread_message_counts(
    session, inbox: Inbox, root_ids: list[int]
) -> dict[int, int]:
    """`root_id -> message count in that thread, in this inbox.`

    3.8.0 paginates the thread view, so the sitemap has to know how
    many pages a thread has, which is `ceil(count / cap)`.

    A separate grouped scan rather than an extra column on
    `_thread_last_activity`, deliberately. Folding it in there was tried
    and reverted: it saves one indexed aggregate (measured ~9.4 ms for
    an lkml-shaped inbox, ~100 ms across all 200 per warm cycle, on an
    hourly cached surface) and costs a function that answers two
    unrelated questions plus a return type five existing lastmod tests
    had to be rewritten around. The scan is not the expensive thing
    here.
    """
    if not root_ids:
        return {}
    return dict(
        session.execute(
            select(ArticleList.thread_root_id, func.count())
            .where(
                ArticleList.inbox_id == inbox.id,
                ArticleList.thread_root_id.in_(root_ids),
            )
            .group_by(ArticleList.thread_root_id)
        ).all()
    )


def _singleton_root_ids(session, inbox: Inbox, root_ids: list[int]) -> set[int]:
    """Of `root_ids`, those whose thread is just themselves in this
    inbox (no reply hangs off them here).

    Deliberately still derived from `thread_parent` rather than from
    `article_lists.thread_root_id`, unlike its sibling
    `_recent_thread_roots_query`. A `GROUP BY thread_root_id HAVING
    count = 1` counts only POPULATED members, so mid-backfill (root
    seeded, replies still NULL) a five-message thread counts as one and
    gets published as a message URL while its own page canonicalises to
    the thread URL, i.e. the sitemap listing a page that disclaims
    itself. BOTH backfill drivers commit one pass at a time (the RPC
    handler submits each as its own WriteOp; the broker's startup path
    drives the same `drive_passes` seam and commits between passes), so
    that state is reachable and committed on every inbox, and durable
    if a run is interrupted.

    The column form also buys nothing measurable here: 3.0-4.1 ms
    against 3.4 ms for this one on 5000 ids, at both 500k and 2.1M
    rows. Faster-looking and wrong is a bad trade.

    Correlated EXISTS, not a join: as a join the planner stops driving
    from `articles IN (ids)` past a few hundred ids and scans every
    article-list row in the inbox instead, i.e. O(ids x inbox rows),
    measured at 3950 ms for 5000 ids over a 50k-row inbox.
    """
    if not root_ids:
        return set()
    child = aliased(Article)
    has_reply = (
        select(child.id)
        .join(ArticleList, ArticleList.article_id == child.id)
        .where(
            child.thread_parent == Article.message_id,
            ArticleList.inbox_id == inbox.id,
            child.id != Article.id,
        )
        .exists()
    )
    with_replies = set(
        session.execute(select(Article.id).where(Article.id.in_(root_ids), has_reply))
        .scalars()
        .all()
    )
    return set(root_ids) - with_replies


def _month_root_ids(
    session: Session, inbox: Inbox, year: int, month: int, *, force: bool = False
) -> list[int]:
    """Ordered article ids of this month's thread roots, as ONE snapshot.

    Every page slices this single list, which is what makes paging
    disjoint and complete. Slicing the DATABASE per page instead
    (`LIMIT`/`OFFSET` per request) is what an earlier version did, and
    it silently loses URLs: pages are separate cache entries filled at
    different times, and `ORDER BY date DESC` means one newly-arrived
    root shifts the whole window, so page 2 re-emits a row page 1
    already had and the row that fell off the end is in no page at all.
    Confirmed in review, not hypothetical, and it converges on exactly
    the bucket paging exists for, since a bulk import clusters into the
    CURRENT month and that month is then both oversized and growing.

    Snapshotting also removes a subtler one. Pages computed under
    different query plans (ANALYZE runs daily and after ingest, and
    per-connection plan caches go stale across it) can order ties
    differently, which review demonstrated dropping 25% of a month's
    URLs. One query, one plan, one order.

    An empty result is NOT cached, and that is a availability
    property rather than tidiness. `year`, `month` and `page` all come
    straight off the URL, so the key space is effectively unbounded and
    unauthenticated; caching misses would let a crawler or a scanner
    turn free 404s into an unbounded stream of writes through the
    broker, which is the single SQLite writer for the whole system and
    is already the contended resource.
    """
    key = f"sitemap:monthids:{inbox.name}:{year:04d}-{month:02d}"
    if not force:
        cached = cache.get(key)
        if cached is not None:
            return cached
    ids = list(session.execute(_month_roots_query(inbox, year, month)).scalars().all())
    if ids:
        cache.set(key, ids, SITEMAP_TTL_SEC)
    return ids


def month_sitemap_xml(
    session: Session,
    inbox: Inbox,
    year: int,
    month: int,
    page: int,
    base: str,
    *,
    force: bool = False,
) -> SitemapPayload | None:
    """One page of one month's thread URLs, or None when there is no
    such page.

    This is W2: the flat per-inbox sitemap caps at
    `SITEMAP_RECENT_PER_INBOX`, so on a corpus this size the entire
    historical tail sat in NO sitemap and was unreachable by
    sitemap-driven discovery. Segmenting by month removes the cap
    without breaching the 50k-per-urlset protocol limit.

    None becomes a 404 at the route. A page beyond the end is honestly
    absent rather than a valid-looking empty document a crawler would
    keep re-fetching, and nothing is cached for it.

    The index's page count and this bounds check are derived from
    separate cache entries with independent TTLs, so for at most one
    TTL after a month's root count changes the index can name a page
    that 404s, or omit one that exists. Bounded and self-correcting;
    the alternative is a shared cache generation, which is a lot of
    machinery for a transient soft error on a sitemap.
    """
    ids = _month_root_ids(session, inbox, year, month, force=force)
    if not ids:
        return None
    offset = (page - 1) * SITEMAP_URLS_PER_PAGE
    if offset >= len(ids):
        return None
    page_ids = ids[offset : offset + SITEMAP_URLS_PER_PAGE]

    def compute() -> SitemapPayload:
        rows = {
            art_id: date
            for art_id, date in session.execute(
                select(Article.id, Article.date).where(Article.id.in_(page_ids))
            ).all()
        }
        # Snapshot order, not query order: the slice is the contract.
        roots = [(i, rows[i]) for i in page_ids if rows.get(i) is not None]
        entries, newest = _sitemap_entries_for_roots(
            roots,
            _singleton_root_ids(session, inbox, page_ids),
            _thread_last_activity(session, inbox, page_ids),
            _thread_message_counts(session, inbox, page_ids),
            unmaterialised_roots(session, inbox.id, page_ids),
            inbox.name,
            base,
        )
        return SitemapPayload(body=_build_sitemap_xml(entries), last_modified=newest)

    return cache.get_or_compute(
        session,
        f"sitemap:month:{inbox.name}:{year:04d}-{month:02d}:{page}",
        SITEMAP_TTL_SEC,
        compute,
        force=force,
    )


def sitemap_index_xml(
    session: Session, base: str, *, force: bool = False
) -> SitemapPayload:
    """Cached body of `/sitemap.xml`. Lists `/meta-sitemap.xml`, one
    `/<inbox>/sitemap.xml` per configured inbox, and
    `/sitemap-maintainers.xml`. Same cache key as the route uses
    (`sitemap:index`) so `warm-cache` can pre-populate it via
    `force=True`. The `last_modified` field of the returned
    `SitemapPayload` is the global-max article date (max across all
    per-inbox lastmods), which drives the `Last-Modified` response
    header at the route layer."""

    def compute() -> SitemapPayload:
        per_inbox_latest = _per_inbox_latest_date(session)
        global_latest = max((d for d in per_inbox_latest.values() if d), default=None)
        entries: list[tuple[str, str | None]] = [
            (f"{base}/meta-sitemap.xml", global_latest),
        ]
        inboxes = session.execute(select(Inbox).order_by(Inbox.name)).scalars().all()
        for inbox in inboxes:
            # The flat per-inbox sitemap stays, as the recent-activity
            # view. It is capped, so it is no longer the whole picture,
            # but crawlers already hold this URL and retiring it would
            # 404 something already indexed for no gain.
            entries.append(
                (
                    f"{base}/{inbox.name}/sitemap.xml",
                    per_inbox_latest.get(inbox.name),
                )
            )
            # Then every month, which is what actually reaches the
            # historical tail. Enumerated per page because sitemaps.org
            # forbids an index referencing another index, so a bucket
            # over the urlset cap cannot hide its pages behind a nested
            # index.
            for year, month, count in _month_root_counts(session, inbox):
                pages = max(1, -(-count // SITEMAP_URLS_PER_PAGE))
                for page in range(1, pages + 1):
                    suffix = "sitemap.xml" if page == 1 else f"sitemap-{page}.xml"
                    # No `<lastmod>` on month entries, deliberately.
                    # The honest value is the newest last-activity among
                    # the month's threads, which needs a per-thread
                    # aggregate over every root in the inbox on every
                    # index build. The cheap substitutes are all
                    # WRONG rather than merely coarse: the month's
                    # newest ROOT date never moves once the month is
                    # past, so it would tell a crawler "unchanged"
                    # precisely when an old thread gains a reply. An
                    # absent lastmod leaves the crawler on its own
                    # schedule; a false one actively suppresses the
                    # re-fetch. Same reasoning as the maintainers
                    # urlset above.
                    entries.append(
                        (
                            f"{base}/{inbox.name}/{year:04d}/{month:02d}/{suffix}",
                            None,
                        )
                    )
        # Reuse global_latest as the maintainers urlset's lastmod: it's
        # free (already computed above) and a reasonable proxy for
        # "maintainer-relevant activity changed" without an extra
        # per-maintainer date query.
        entries.append((f"{base}/sitemap-maintainers.xml", global_latest))
        return SitemapPayload(
            body=_build_sitemap_index_xml(entries),
            last_modified=global_latest,
        )

    return cache.get_or_compute(
        session,
        "sitemap:index",
        SITEMAP_TTL_SEC,
        compute,
        force=force,
    )


def meta_sitemap_xml(
    session: Session, base: str, *, force: bool = False
) -> SitemapPayload:
    """Cached body of `/meta-sitemap.xml`. One-URL urlset covering `/`
    with the global-max article date as lastmod. The `last_modified`
    field carries the same global-max date so the route's
    `Last-Modified` header stays in sync with the body's `<lastmod>`."""

    def compute() -> SitemapPayload:
        per_inbox_latest = _per_inbox_latest_date(session)
        global_latest = max((d for d in per_inbox_latest.values() if d), default=None)
        return SitemapPayload(
            body=_build_sitemap_xml([(base + "/", global_latest)]),
            last_modified=global_latest,
        )

    return cache.get_or_compute(
        session,
        "sitemap:meta",
        SITEMAP_TTL_SEC,
        compute,
        force=force,
    )


def maintainers_sitemap_xml(
    session: Session, base: str, *, force: bool = False
) -> SitemapPayload:
    """Cached body of `/sitemap-maintainers.xml`. One-urlset listing
    every maintainer's profile page (`/maintainers/<address>`).

    Slice-1 decision: no per-url `<lastmod>` and `last_modified=None`
    on the returned payload, so the route emits a plain 200 with no
    `Last-Modified` header. `all_maintainers` doesn't carry a
    per-maintainer "last active" date, and deriving one would mean a
    date query per maintainer (~4000 on the production MAINTAINERS
    corpus) just to populate a freshness hint crawlers treat as
    secondary to discovery. Revisit if maintainer profile pages ever
    gain a cheap last-changed signal."""

    def compute() -> SitemapPayload:
        entries: list[tuple[str, str | None]] = [
            (base + maintainer_path(addr), None)
            for addr, _name in all_maintainers(session)
        ]
        return SitemapPayload(body=_build_sitemap_xml(entries), last_modified=None)

    return cache.get_or_compute(
        session,
        "sitemap:maintainers",
        SITEMAP_TTL_SEC,
        compute,
        force=force,
    )


def inbox_sitemap_xml(
    session: Session, inbox: Inbox, base: str, *, force: bool = False
) -> SitemapPayload:
    """Cached body of `/<inbox>/sitemap.xml`. Dashboard + year/month
    archives that actually have data + the SITEMAP_RECENT_PER_INBOX
    most-recent article URLs in this inbox. The `last_modified` field
    of the returned `SitemapPayload` is this inbox's most-recent
    article date, scoped so updates to other inboxes don't trigger
    Google to re-fetch this one."""

    def compute() -> SitemapPayload:
        entries: list[tuple[str, str | None]] = []

        inbox_latest_dt = session.scalar(
            select(func.max(Article.date))
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date.is_not(None),
            )
        )
        inbox_latest = inbox_latest_dt.strftime("%Y-%m-%d") if inbox_latest_dt else None
        entries.append((f"{base}/{inbox.name}/", inbox_latest))

        # Distinct (year, month) pairs that actually have data, in
        # one round-trip. Empty months are skipped, they'd 200
        # with a "no messages" page, but the sitemap is for
        # discovery surfaces with real content.
        year_month_rows = session.execute(
            select(
                func.strftime("%Y", Article.date).label("y"),
                func.strftime("%m", Article.date).label("m"),
            )
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date.is_not(None),
            )
            .group_by("y", "m")
            .order_by("y", "m")
        ).all()
        years_with_data: set[str] = {y for y, _ in year_month_rows}
        for y in sorted(years_with_data, reverse=True):
            entries.append((f"{base}/{inbox.name}/{y}/", None))
        for y, m in sorted(year_month_rows, reverse=True):
            entries.append((f"{base}/{inbox.name}/{y}/{m}/", None))

        # The subsystem index plus the dashboards it links. These are
        # distinctive hub pages nothing else mirrors (per-subsystem
        # recent patches, threads and reviewers, derived from
        # MAINTAINERS), and until now they were in no sitemap at all.
        #
        # The ACTIVE set only, which is the same cached payload the
        # dashboard widget and the index page read: `most_active_...`
        # is capped at `MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP` (100) per
        # inbox, measuring 3,394 (inbox, subsystem) pairs in total on
        # the production corpus 2026-07-29. The full MAINTAINERS
        # taxonomy would instead be ~3,300 sections x ~200 inboxes,
        # i.e. ~660,000 URLs, almost all of them a page for a
        # subsystem no patch in that inbox has ever touched.
        #
        # `compute_on_miss=False` matters: this builder runs on the
        # request path on a cold sitemap cache, and that aggregation is
        # multi-second per inbox (the 2.8.0 regression family). Warm
        # ordering makes the miss rare rather than routine, since
        # `most_active_subsystems_in_inbox` is warmed immediately
        # before `sitemap:inbox:<name>` in the same slow-tier per-inbox
        # target list. A miss omits these entries for one cycle; it
        # must never turn a sitemap render into a minutes-long compute.
        seen_subsystem_locs: set[str] = set()
        for activity in most_active_subsystems_in_inbox(
            session,
            inbox,
            days=7,
            limit=MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP,
            compute_on_miss=False,
        ):
            # Skip names the route would refuse to serve back. The
            # emitter and the route are different modules and nothing
            # else forces them to agree, so without this the sitemap
            # hands crawlers URLs that 404 (production carried three
            # such names on 2026-07-29).
            if not is_addressable_subsystem_name(activity.name):
                continue
            # `inbox.name`, never `activity.inbox_name`. They are equal
            # for THIS helper, but the cross-inbox variant sets
            # `inbox_name` to whichever inbox the subsystem is busiest
            # in, so using it would put another inbox's URL in this
            # inbox's sitemap the moment anyone reuses this loop.
            loc = base + subsystem_path(inbox.name, activity.name)
            # `subsystems.name` has no unique constraint and the path is
            # lowercased, so two sections differing only in case collapse
            # to one URL. Emitting it twice is a duplicate <loc> in a
            # single urlset.
            if loc in seen_subsystem_locs:
                continue
            seen_subsystem_locs.add(loc)
            # Date-only string, like every other entry here: the list
            # is `(loc, lastmod-as-str)` and `_build_sitemap_xml` sets
            # the element text verbatim, so handing it a datetime is a
            # serialize-time TypeError, i.e. a 500 on the whole sitemap.
            entries.append(
                (
                    loc,
                    (
                        activity.last_activity.strftime("%Y-%m-%d")
                        if activity.last_activity
                        else None
                    ),
                )
            )
        # The index page, listed only when it is not empty. It renders a
        # bare "nothing active here" for the ~45-of-200 production
        # inboxes whose recent traffic touches no MAINTAINERS-claimed
        # path, and advertising those to crawlers is the same thin-page
        # trade this change set refuses everywhere else. It stays linked
        # from the inbox dashboard either way, so it is discoverable the
        # moment it has something to show.
        if seen_subsystem_locs:
            entries.append((f"{base}/{inbox.name}/subsystem/", None))

        # Recent THREADS in the inbox, one URL per thread, pointing at
        # the whole-thread view. Individual message pages canonicalise
        # there (see `mimir.web.routes.message`), and a sitemap should
        # list canonical URLs, so listing per-message URLs here would
        # hand crawlers ~10x the URLs only to redirect their attention
        # elsewhere. One fat thread document per entry is also the
        # whole point of the consolidation.
        #
        # Roots come from the materialised column; see
        # `_recent_thread_roots_query` for the plan shape, the
        # measurements, and why NULL rows are absent rather than listed
        # wrongly. Kept as a pointer rather than a second copy of the
        # numbers, because a duplicated measurement is one that goes
        # stale. Rows still awaiting the backfill
        # carry NULL, fail the comparison, and are simply absent until
        # it reaches them: freshness, not correctness.
        #
        # `_singleton_root_ids` deliberately does NOT use the column;
        # see its docstring for why counting populated members
        # misclassifies mid-backfill.
        #
        # Cross-posted articles still appear in each linked inbox's
        # sitemap; the page's own canonical resolves which to keep.
        recent_roots = session.execute(
            _recent_thread_roots_query(inbox).limit(SITEMAP_RECENT_PER_INBOX)
        ).all()
        # Which URL to list has to match what that page's own canonical
        # says, or the sitemap publishes a page that disclaims itself.
        # A single-message thread's canonical is the MESSAGE page (it is
        # already the whole conversation, and carries the patch surfaces
        # `/t` omits), so listing `/t` there would publish the thinner
        # of two self-canonical near-duplicates and drop the canonical
        # one entirely. Mirrors the `len(thread) > 1` rule in
        # `mimir.web.routes.message`.
        singleton_ids = _singleton_root_ids(
            session, inbox, [art_id for art_id, _ in recent_roots]
        )
        last_activity = _thread_last_activity(
            session, inbox, [art_id for art_id, _ in recent_roots]
        )
        recent_root_ids = [art_id for art_id, _ in recent_roots]
        root_entries, _newest = _sitemap_entries_for_roots(
            recent_roots,
            singleton_ids,
            last_activity,
            _thread_message_counts(session, inbox, recent_root_ids),
            unmaterialised_roots(session, inbox.id, recent_root_ids),
            inbox.name,
            base,
        )
        entries.extend(root_entries)
        return SitemapPayload(
            body=_build_sitemap_xml(entries),
            last_modified=inbox_latest,
        )

    return cache.get_or_compute(
        session,
        f"sitemap:inbox:{inbox.name}",
        SITEMAP_TTL_SEC,
        compute,
        force=force,
    )
