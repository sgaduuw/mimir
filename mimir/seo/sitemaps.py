"""Sitemap XML for crawlers.

Cached surfaces feeding `/sitemap.xml`, `/messages-sitemap.xml`,
`/meta-sitemap.xml`, `/sitemap-maintainers.xml`,
`/sitemap-series-diffs-<h>.xml`, `/<inbox>/sitemap.xml` and the archive
pages. Cache keys are stable; bumping `cache.NAMESPACE_VERSION`
invalidates everything if a payload shape changes, so per-route expiry
is purely about freshness.

The archive is two trees since #964: each year or month file lists `/t`
URLs, and its `messages-` sibling lists message URLs, both cut from one
build by `_split_by_kind`. `/sitemap.xml` indexes the first tree and the
pages mimir adds over lore.kernel.org; `/messages-sitemap.xml` indexes
the second.

Each surface returns a `SitemapPayload` carrying both the XML body and
the most-recent `<lastmod>` date represented in that body. That date is
an in-body signal only: it goes into `<lastmod>` elements, NOT into an
HTTP `Last-Modified` header. Sitemap responses deliberately carry no
conditional-GET validator and always answer 200, because a content-date
validator lies about structural changes (adding a urlset to the index
moves no article date) and 3.6.0 pinned Cloudflare's edge on a stale
index for hours that way. Freshness rides `<lastmod>` plus
`Cache-Control: max-age`, which can delay but never pin. See CONTEXT.md
"SEO posture" before reintroducing a validator here.
"""

import json
import logging
import zlib
from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as date_cls
from functools import partial
from itertools import groupby
from typing import NamedTuple
from xml.etree.ElementTree import Element, SubElement, tostring

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from mimir import cache
from mimir.config import settings
from mimir.maintainer_directory import (
    all_maintainers,
    is_addressable_maintainer_address,
    maintainer_path,
)
from mimir.models import Article, ArticleList, Inbox
from mimir.patch_series import adjacent_versions, series_head, slot_rank
from mimir.patches import article_has_files
from mimir.subsystems import (
    is_addressable_subsystem_name,
    subsystem_index_path,
    subsystem_path,
)
from mimir.subsystems_dashboard import (
    MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP,
    most_active_subsystems_in_inbox,
)
from mimir.threading import unmaterialised_roots

logger = logging.getLogger(__name__)

# URLs per month sitemap page.
#
# TWO ceilings apply and the smaller one is not the obvious one:
#
#   1. sitemaps.org allows 50,000 URLs per urlset. This is the ceiling
#      everyone reaches for, and it is not the binding one.
#   2. SQLite's SQLITE_LIMIT_VARIABLE_NUMBER is 32,766 on this build
#      (measured 2026-07-30, sqlite 3.50.4). A page slice is passed
#      whole to five call sites as an expanding `IN` list (six
#      statements: `unmaterialised_roots` issues two), so
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
#
# This width bounds ROOTS per page, and since thread pagination shipped
# a root no longer costs exactly one URL: it costs
# `ceil(messages / THREAD_VIEW_RENDER_CAP)`. So the headroom to the
# 50,000 protocol limit is what has to be re-checked, not the root
# count.
#
# Measured against production 2026-08-03, on the population this file
# actually emits (roots whose OWN date falls in the bucket, and every
# message in the threads they head, regardless of that message's date):
# the widest bucket is `git` 2016-06 at **89,679 roots / 288,499
# messages**, whose largest single thread is 205 messages. Expanded at
# cap 75 that is **89,734 URLs**, i.e. 55 more than the root count,
# +0.06%. A full 20,000-root slice therefore emits about 20,012.
#
# State the population, not just the number. An earlier version of this
# note said 89,946 roots, which came from grouping `article_lists` by
# each ARTICLE's month: that counts roots whose threads saw activity in
# the month rather than roots dated in it, which is not what
# `_month_root_ids` selects. Same 2026-07-28 trap as the "6M rows"
# figure, one level subtler, because the number was freshly measured
# and still counted the wrong thing.
#
# The earlier bound (`20,000 + messages/75`) was arithmetically valid
# and useless: it assumed one slice could hold every message in the
# bucket, giving 23,844 where the real answer is 20,012. Kept out of
# the file rather than corrected in place, because a loose bound
# invites re-deriving from it.
#
# Re-measure if this width, the render cap, or the ceiling changes.
# `_warn_if_over_cap` makes a breach loud rather than silent, so a
# wrong derivation here costs a log line, not a rejected sitemap.
SITEMAP_URLS_PER_PAGE = 20000
# sitemaps.org caps a single urlset at 50,000 URLs.
SITEMAP_MAX_URLS = 50000
SITEMAP_TTL_SEC = 3600


@dataclass
class SitemapPayload:
    """The body + last-modified pair for one sitemap surface. The
    last_modified field is an ISO-8601 date string (`YYYY-MM-DD`) or
    None when the sitemap has no datable content. It reaches crawlers
    only through `<lastmod>` in the body; routes do NOT project it into
    a `Last-Modified` header (see the module docstring). The cache
    (`cache.register("SitemapPayload", ...)` below) stores the full
    dataclass so a warmed body + lastmod are always consistent."""

    body: str
    last_modified: str | None


cache.register("SitemapPayload", SitemapPayload)


# What `_sitemap_entries_for_roots` took for a root: the thread view, or
# the root's own message page. It is the ONLY place that decides, and it
# says so here so a caller filters on the tag instead of re-deriving it.
KIND_THREAD = "t"
KIND_MESSAGE = "message"

# `<priority>` is our own signal: Google ignores it. Values are the
# table in #964, ordered by how much a page adds over lore.kernel.org.
PRIORITY_SITE = 1.0  # `/`, `/merge-window`, `/llms.txt`
# The meta sitemap lists it and robots.txt's `LLM-Context:` names it (#969).
LLMS_TXT_PATH = "/llms.txt"
PRIORITY_PAGE = 0.9  # `/t` pages, series diffs, subsystem dashboards
PRIORITY_HUB = 0.7  # inbox dashboards, maintainer pages
PRIORITY_MESSAGE = 0.3
PRIORITY_MONTH = 0.2
PRIORITY_YEAR = 0.1


class SitemapEntry(NamedTuple):
    """One `<url>` or `<sitemap>`. `kind` never reaches the XML: it tags
    thread-root entries for whoever filters them (see `KIND_THREAD`)."""

    loc: str
    lastmod: str | None
    priority: float | None = None
    kind: str | None = None


def _warn_if_over_cap(entries: list, kind: str) -> None:
    """Both sitemap shapes cap at 50,000 entries, and a breach is silent
    by construction: the document renders, the route serves 200, and
    only the crawler discovers it is unusable. Every caller derives its
    entry count from a different query, so the builders are the one
    place that sees the number that actually binds."""
    if len(entries) > SITEMAP_MAX_URLS:
        logger.warning(
            "sitemap %s over protocol cap: %d entries (max %d); "
            "crawlers will reject this document",
            kind,
            len(entries),
            SITEMAP_MAX_URLS,
        )


def _build_xml(
    root_tag: str, child_tag: str, entries: list[tuple[str, str | None]]
) -> str:
    """Render a sitemaps.org document: a `<urlset>` of `<url>` for a
    sitemap, a `<sitemapindex>` of `<sitemap>` for the index. Each
    entry is `(loc, lastmod | None)` or a `SitemapEntry`; when `lastmod`
    or `priority` is None the element is omitted. Only urlsets get a
    priority: the index never does, as the protocol defines none there.
    Caller formats the timestamp, date-only
    `YYYY-MM-DD` is what Google's docs recommend for crawl-scheduling
    and is what mimir emits.

    The over-cap warning exists because a breach is silent by
    construction: the document renders, the route serves 200, and the
    crawler discards it. Every caller derives its own entry count from
    a different query, so this is the one place that sees the number
    that actually matters. The index is the surface with LESS headroom:
    sitemaps.org caps it at 50,000 `<sitemap>` entries too, and it held
    3,542 on 2026-10-07 (7% of the limit, after year bucketing; see
    `_month_counts`) against a worst-case urlset of about 20,012 (40%).
    It is also the one that grows
    without bound: every calendar month adds about one entry per inbox,
    and an `admin inbox add` for a historically-imported list can add
    hundreds at once.
    """
    _warn_if_over_cap(entries, root_tag)
    root = Element(root_tag, xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for entry in entries:
        loc, lastmod, priority, _kind = SitemapEntry(*entry)
        el = SubElement(root, child_tag)
        SubElement(el, "loc").text = loc
        if lastmod:
            SubElement(el, "lastmod").text = lastmod
        if priority is not None:
            SubElement(el, "priority").text = f"{priority:.1f}"
    return '<?xml version="1.0" encoding="utf-8"?>\n' + tostring(
        root, encoding="unicode"
    )


_build_sitemap_xml = partial(_build_xml, "urlset", "url")
_build_sitemap_index_xml = partial(_build_xml, "sitemapindex", "sitemap")


def _split_by_kind(
    entries: list[SitemapEntry],
) -> tuple[SitemapPayload, SitemapPayload]:
    """`(thread, message)` payloads of one archive file, partitioned on
    the kind tag alone (#964). The tag is `_sitemap_entries_for_roots`'
    decision; reading anything else here, such as the URL's shape, would
    be a second predicate free to disagree with the pages' canonicals.
    Either half may be empty: the index names both files of a bucket, so
    an empty half is an empty urlset rather than a 404."""
    return tuple(
        SitemapPayload(
            body=_build_sitemap_xml(part),
            last_modified=max((e.lastmod for e in part if e.lastmod), default=None),
        )
        for part in (
            [e for e in entries if e.kind == KIND_THREAD],
            [e for e in entries if e.kind == KIND_MESSAGE],
        )
    )


def _per_inbox_latest_date(session) -> dict[str, str | None]:
    """Newest article date per inbox, as `YYYY-MM-DD` strings (or None
    when the inbox is empty). Feeds the sitemap index `<lastmod>` per
    sub-sitemap.

    Reads `Inbox.last_article_date`, which ingest raises to the newest
    date of every article it adds or cross-links. It matched
    `max(Article.date)` over each inbox's members for all 203
    production inboxes on 2026-10-07, and costs nothing, where that
    aggregate looked up every member's article and took 15 s (#640).
    The column never decreases, so an article removed from an inbox
    does not lower it."""
    rows = session.execute(select(Inbox.name, Inbox.last_article_date)).all()
    return {name: (dt.strftime("%Y-%m-%d") if dt else None) for name, dt in rows}


def _sitemap_entries_for_roots(
    roots: list[tuple[int, datetime]],
    singleton_ids: set[int],
    last_activity: dict[int, datetime],
    message_counts: dict[int, int],
    unrankable_roots: set[int],
    inbox_name: str,
    base: str,
) -> list[SitemapEntry]:
    """Tagged entries for thread roots.

    Each entry carries the branch taken, `KIND_THREAD` or
    `KIND_MESSAGE`, and the priority that kind ranks at. Callers read
    the tag; none may re-ask which branch a root takes.

    The one place the `/t`-versus-message decision and the lastmod pick
    are made. A second copy would be a place to drift, on a surface where
    drift is silent: the sitemap would list a URL whose own page names a
    different canonical.

    A singleton's lastmod is its own date by definition, said
    explicitly rather than left to the aggregate, which keeps this
    coherent with `_singleton_root_ids` BY CONSTRUCTION. That function
    reads `thread_parent` while the aggregate reads the materialised
    column, so a wrong column value could otherwise make a
    single-message page, which can never change, advertise another
    thread's date. For correct data the branch is a no-op.
    """
    # Function-local, NOT module-level: `mimir.web` imports this package
    # at module load, so a top-level import here closes the cycle and
    # any entry point that reaches `mimir.seo` before `mimir.web` dies
    # with a partially-initialised-module ImportError. `json_ld.py` and
    # `atom.py` have done it this way all along and the package
    # docstring says so; this file briefly did not, which is what
    # `tests/test_import_graph.py` now pins.
    from mimir.web.urls import _msg_path, thread_page_url

    cap = settings.thread_view_render_cap
    entries: list[SitemapEntry] = []
    for art_id, date in roots:
        loc = base + _msg_path(art_id, date, inbox_name)
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
            # The lastmod is the ROOT's date, not the thread's newest
            # activity, and for an unrankable thread that is a
            # deliberate understatement rather than an oversight. The
            # activity aggregate is computed FROM the materialised
            # column, which is the thing just declared unreliable for
            # this root, so trusting it here would launder an untrusted
            # value into a freshness claim. An understated lastmod
            # delays a re-crawl; the alternative can assert a date the
            # thread never had. Self-corrects when the backfill
            # completes and the root leaves this branch.
            stamp = date.strftime("%Y-%m-%d")
            entries.append(SitemapEntry(loc, stamp, PRIORITY_MESSAGE, KIND_MESSAGE))
        else:
            stamp = (activity or date).strftime("%Y-%m-%d")
            # EVERY page, not just the first. Since 3.8.0 the thread
            # view is paginated and each page is self-canonical, so a
            # page absent from the sitemap is reachable only by walking
            # the `next` chain, one hop deeper each time. Messages on it
            # canonicalise TO it, which would leave them deferring to a
            # URL crawlers never fetch.
            #
            # Measured against production 2026-08-03: **16,726** of
            # **6,074,908** thread roots hold more than 75 messages and
            # so paginate, adding **24,753** URLs, or +0.41%. (The
            # denominator counts every root's advertised URL, most of
            # which are singleton MESSAGE urls rather than `/t`; it is
            # the right denominator for "how much does pagination add
            # to the sitemap", which is the question being asked.) An earlier version of this note
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
                entries.append(
                    SitemapEntry(
                        base + thread_page_url(art_id, date, inbox_name, pg),
                        stamp,
                        PRIORITY_PAGE,
                        KIND_THREAD,
                    )
                )
    return entries


def _archive_bounds(year: int, month: int | None) -> tuple[datetime, datetime]:
    """Half-open `[start, end)` for a month or whole year, in UTC.

    A range predicate rather than `strftime(...) = '2024-06'` so the
    date index stays usable: wrapping the column in a function makes it
    unsargable, which is the same trap CONTEXT.md records for
    `LIKE 'prefix%' ESCAPE` on `article_files.path`.
    """
    start = datetime(year, month or 1, 1, tzinfo=UTC)
    end = (
        datetime(year + 1, 1, 1, tzinfo=UTC)
        if month is None or month == 12
        else datetime(year, month + 1, 1, tzinfo=UTC)
    )
    return start, end


def _archive_lastmod(
    year: int, month: int | None, today: str, newest: str | None
) -> str | None:
    """`<lastmod>` of a month or year archive page, as `YYYY-MM-DD`.

    A closed period (last day before `today`) can no longer gain
    messages, so it is dated its last day. The current period is still
    changing, so it gets the inbox's newest message date, capped at
    `today`, which is never past the period's last day here: a page
    cannot have changed in the future, and one mis-dated mail (lore
    carries 2037s) would otherwise date the current month and year after
    today. Without a newest date it gets none, rather than a guess. ISO
    dates compare as strings.
    """
    last_day = (_archive_bounds(year, month)[1] - timedelta(days=1)).date().isoformat()
    if last_day < today:
        return last_day
    return min(newest, today) if newest else None


def _archive_roots_query(inbox: Inbox, year: int, month: int | None):
    """Thread roots whose own date falls in this inbox-month or inbox-year.

    Ordered deterministically, because pages are `LIMIT`/`OFFSET`
    slices of it and an unstable order would drop and duplicate URLs
    across page boundaries.
    """
    start, end = _archive_bounds(year, month)
    if month is not None:
        # Months keep the date-driven plan on purpose. The inbox-driven
        # form below costs a whole inbox scan whatever the period. In a
        # local synthetic bench (5.4M rows, about 30% of production
        # scale, production planner statistics, 2026-10-26) a month on a
        # 2.5M-member inbox went from 38 ms to 271 ms per child, 7x
        # worse. This form costs one month of every inbox's articles.
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
    # A year is driven from this inbox's roots (#947). A bare date range
    # starts at `ix_articles_date` across every inbox's articles for the
    # year. In the same local synthetic bench (about 30% of production
    # scale, measured 2026-10-26) that cost 280 to 440 ms per child
    # whatever the inbox. The grouped subquery is a co-routine SQLite
    # cannot flatten, so it scans the covering
    # `ix_article_lists_thread_root` for this inbox first: an
    # 8k-member inbox went from 283 ms to 1.4 ms. `own` keeps the old
    # predicate: a root must be its own member.
    roots = (
        select(ArticleList.thread_root_id.label("root"))
        .where(
            ArticleList.inbox_id == inbox.id, ArticleList.thread_root_id.is_not(None)
        )
        .group_by(ArticleList.thread_root_id)
        .subquery()
    )
    own = aliased(ArticleList)
    return (
        select(Article.id, Article.date)
        .select_from(roots)
        .join(Article, Article.id == roots.c.root)
        .join(
            own,
            (own.article_id == Article.id)
            & (own.inbox_id == inbox.id)
            & (own.thread_root_id == Article.id),
        )
        .where(Article.date >= start, Article.date < end)
        .order_by(Article.date.desc(), Article.id.desc())
    )


def _month_counts(session, inbox: Inbox) -> list[tuple[int, int, int, int]]:
    """`(year, month, root_count, url_count)` per month that has dated
    roots, oldest first. `url_count` is the thread pages those roots
    expand to at the current render cap, a conservative total:
    unrankable threads may render fewer URLs, and overcounting only
    keeps monthly discovery for those years.

    Drives the index: years fitting one root page use a year sitemap;
    larger years contribute `ceil(count / SITEMAP_URLS_PER_PAGE)`
    entries per month. The index must count before naming pages. Nesting is not an
    escape: GOOGLE rejects a nested index ("Incorrect sitemap index
    format: Nested sitemap indexes"), so the pages must be enumerated
    here.

    The authority matters because this file used to credit
    sitemaps.org, which in fact permits nesting outright ("You can have
    more than one Sitemap index file"). The conclusion is unchanged,
    but a reader checking the protocol would have found the opposite of
    what the comment claimed and reasonably concluded the constraint
    was imaginary.

    Cost. One query per inbox groups members by `thread_root_id` on
    the covering `ix_article_lists_thread_root`, then reads one date per
    root. Measured in production on 2026-10-07 across 203 inboxes: 9.7 s,
    against 17.5 s for root counts alone when every member's article was
    read to find the roots, plus up to 9.7 s more for a second query
    that produced the URL counts. Both forms gave identical root counts
    for every inbox (#640).

    Index size. The last figure counted on a SERVED document is
    **32,629 entries (2.6 MiB) on 2026-09-29**, of which 32,420 were
    month buckets: 65% of the protocol's 50k-per-index limit. That
    is the PRE-COARSENING shape, measured against 3.8.1, and it is
    what year bucketing exists to fix; it is kept as the baseline the
    served count below is compared against.

    With year bucketing deployed, the production index held 3,542
    entries on 2026-10-07, 7% of the cap (modelled beforehand at
    ~3,330).

    Growth rate is the wrong thing to plan from. 205 inboxes x 258
    months is ~52,900 POSSIBLE buckets, already past the cap, so the
    ceiling is bounded by which buckets hold data rather than by
    time: one `admin inbox add` for a historically imported list can
    add hundreds of entries at once, which no per-year figure
    predicts.

    Per-inbox SUB-INDEXES were never the fix, despite what an
    earlier version of this docstring proposed. That is an index
    referencing an index, which is the nesting GOOGLE rejects (see
    `_month_counts` above and CONTEXT.md "SEO posture"); it is
    the same shape already tried and rejected for the maintainers
    layer. Year bucketing avoids the question entirely by shrinking
    the flat index instead of structuring it. What the protocol does
    allow, if the ceiling is ever approached again, is more than one
    INDEPENDENT index file, none referencing another. See #551.
    """
    members = (
        select(ArticleList.thread_root_id.label("root"), func.count().label("n"))
        .where(
            ArticleList.inbox_id == inbox.id, ArticleList.thread_root_id.is_not(None)
        )
        .group_by(ArticleList.thread_root_id)
        .subquery()
    )
    cap = settings.thread_view_render_cap
    y = func.strftime("%Y", Article.date)
    m = func.strftime("%m", Article.date)
    rows = session.execute(
        select(y, m, func.count(), func.sum((members.c.n + cap - 1) // cap))
        .select_from(members)
        .join(Article, Article.id == members.c.root)
        .where(Article.date.is_not(None))
        .group_by(y, m)
        .order_by(y, m)
    ).all()
    return [(int(yy), int(mm), n, urls) for yy, mm, n, urls in rows]


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
    sizes: 9.4 ms for an lkml-shaped inbox at 5000 roots (the size of
    the recent-thread list it was measured for, removed in #964), 0.47 ms
    for a small inbox (553 roots). EXPLAIN uses
    `ix_article_lists_thread_root` on BOTH columns. The
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
    `article_lists.thread_root_id`, unlike the own-root test in
    `_archive_roots_query`. A `GROUP BY thread_root_id HAVING
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
    different query plans (ANALYZE runs weekly and after ingest, and
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
    ids = list(
        session.execute(_archive_roots_query(inbox, year, month)).scalars().all()
    )
    if ids:
        cache.set(key, ids, SITEMAP_TTL_SEC)
    return ids


def _archive_entries(
    session: Session, inbox: Inbox, page_ids: list[int], base: str
) -> list[SitemapEntry]:
    rows = {
        art_id: date
        for art_id, date in session.execute(
            select(Article.id, Article.date).where(Article.id.in_(page_ids))
        ).all()
    }
    # Snapshot order, not query order: the slice is the contract.
    roots = [(i, rows[i]) for i in page_ids if rows.get(i) is not None]
    return _sitemap_entries_for_roots(
        roots,
        _singleton_root_ids(session, inbox, page_ids),
        _thread_last_activity(session, inbox, page_ids),
        _thread_message_counts(session, inbox, page_ids),
        unmaterialised_roots(session, inbox.id, page_ids),
        inbox.name,
        base,
    )


# The `split` keys hold both trees of a file, `(thread, message)`. New
# names rather than a `NAMESPACE_VERSION` bump: only these rows changed.
def _year_pages_key(inbox_name: str, year: int) -> str:
    return (
        f"sitemap:yearsplit:{inbox_name}:{year:04d}:{settings.thread_view_render_cap}"
    )


def _month_page_key(inbox_name: str, year: int, month: int, page: int) -> str:
    return (
        f"sitemap:monthsplit:{inbox_name}:{year:04d}-{month:02d}:{page}"
        f":{settings.thread_view_render_cap}"
    )


def _unpack_year_pages(packed: str) -> list[tuple[SitemapPayload, SitemapPayload]]:
    return [
        (SitemapPayload(*row[:2]), SitemapPayload(*row[2:]))
        for row in json.loads(zlib.decompress(b64decode(packed)))
    ]


def _year_sitemap_xml(
    session: Session, inbox: Inbox, year: int, page: int, base: str, *, force: bool
) -> tuple[SitemapPayload, SitemapPayload] | None:
    """Cache all yearly pages as one snapshot of expanded URLs.

    Eligibility is only an index-size choice. Replies can arrive after that
    check, so serving must bound actual URLs independently. Keep page bodies
    together: independently refreshed pages could drop URLs between slices.
    Pages are cut from both trees' URLs together and then split, so the
    two trees share page numbers and each pair is one pre-split page.
    """
    key = _year_pages_key(inbox.name, year)
    packed = None if force else cache.get(key)
    pages = _unpack_year_pages(packed) if packed is not None else None
    if pages is None:
        ids = list(session.scalars(_archive_roots_query(inbox, year, None)))
        entries = []
        # Bound SQL IN lists even if an originally small year grows.
        for offset in range(0, len(ids), SITEMAP_URLS_PER_PAGE):
            entries.extend(
                _archive_entries(
                    session, inbox, ids[offset : offset + SITEMAP_URLS_PER_PAGE], base
                )
            )
        pages = [
            _split_by_kind(entries[offset : offset + SITEMAP_MAX_URLS])
            for offset in range(0, len(entries), SITEMAP_MAX_URLS)
        ]
        if pages:
            # Cache the valid year even on a missing page: its key excludes
            # the requested page, so repeated 404s cannot force a full rebuild.
            # Whole-year XML can exceed the cache's 8 MiB value ceiling even
            # when each page fits. Repeated XML/URL prefixes compress well.
            packed = b64encode(
                zlib.compress(
                    json.dumps(
                        [
                            (t.body, t.last_modified, m.body, m.last_modified)
                            for t, m in pages
                        ]
                    ).encode()
                )
            ).decode("ascii")
            cache.set(key, packed, SITEMAP_TTL_SEC)
    return pages[page - 1] if page <= len(pages) else None


def archive_sitemap_xml(
    session: Session,
    inbox: Inbox,
    year: int,
    month: int | None,
    page: int,
    base: str,
    *,
    messages: bool = False,
    force: bool = False,
) -> SitemapPayload | None:
    """One page of a month or year's `/t` URLs, or of its message URLs
    when `messages`, or None when there is no such page. Both trees come
    from one build and one cache row, so they always agree on the pages.

    This is W2, and since #964 the only sitemap surface that lists thread
    and message pages. The per-inbox file once listed the most recent
    5,000 roots, which left the historical tail in no sitemap at all.
    Segmenting by month covers every root without breaching the
    50k-per-urlset protocol limit.

    None becomes a 404 at the route. A page beyond the end is honestly
    absent rather than a valid-looking empty document a crawler would
    keep re-fetching. No missing-page key is cached; a nonempty yearly
    snapshot is retained so repeated misses can reuse the work.

    The index's page count and this bounds check are derived from
    separate cache entries with independent TTLs, so for at most one
    TTL after a month's root count changes the index can name a page
    that 404s, or omit one that exists. Bounded and self-correcting;
    the alternative is a shared cache generation, which is a lot of
    machinery for a transient soft error on a sitemap.
    """
    if month is None:
        pair = _year_sitemap_xml(session, inbox, year, page, base, force=force)
    else:
        # A forced build re-reads the root list on page 1 only. The warm
        # walks pages in order, so later pages slice page 1's snapshot: a
        # fresh read per page would let a root arriving mid-warm shift the
        # window, listing one root twice and dropping another.
        ids = _month_root_ids(session, inbox, year, month, force=force and page == 1)
        offset = (page - 1) * SITEMAP_URLS_PER_PAGE
        if offset >= len(ids):
            return None
        page_ids = ids[offset : offset + SITEMAP_URLS_PER_PAGE]
        pair = cache.get_or_compute(
            session,
            _month_page_key(inbox.name, year, month, page),
            SITEMAP_TTL_SEC,
            lambda: _split_by_kind(_archive_entries(session, inbox, page_ids, base)),
            force=force,
        )
    return None if pair is None else pair[1 if messages else 0]


def _archive_files(session: Session, inbox: Inbox) -> list[tuple[int, int | None, int]]:
    """`(year, month or None, page)` of every archive file the index
    names for this inbox, oldest first. A year fitting one root page and
    the URL cap is one year file (only its page 1 is named); any other
    year is named month by month, every page. One `_month_counts` pass.

    Shared by the index and the slow-tier warm of the latest periods, so
    the warm fills exactly the files, and cache keys, the index reads.
    """
    files: list[tuple[int, int | None, int]] = []
    # One grouped aggregate per inbox, not two queries per year.
    # ponytail: this buys headroom, not an absolute index-size bound;
    # use multiple top-level indexes if the 50k alarm is approached.
    for year, months in groupby(_month_counts(session, inbox), key=lambda r: r[0]):
        buckets = list(months)
        if (
            1 <= year <= 9998
            and sum(count for _, _, count, _ in buckets) <= SITEMAP_URLS_PER_PAGE
            and sum(urls for _, _, _, urls in buckets) <= SITEMAP_MAX_URLS
        ):
            files.append((year, None, 1))
            continue
        for _, month, count, _ in buckets:
            pages = max(1, -(-count // SITEMAP_URLS_PER_PAGE))
            files.extend((year, month, page) for page in range(1, pages + 1))
    return files


def warm_latest_archive_sitemaps(
    session: Session, inbox: Inbox, base: str, today: date_cls
) -> None:
    """Rebuild this inbox's archive files for the current and the previous
    period, every page and both trees, under the keys the index reads.

    The newest threads are listed only there, and the index dates a
    child from its cache row alone, so an unwarmed file goes undated
    until a crawler happens to fetch it (#964 slice 3, which dropped the
    warmed recent-thread list this replaces). The previous period too,
    because replies keep arriving in last month's threads after the month
    turns; in January that is last year's files. `today` comes from the
    caller, so a whole warm agrees on one date. `force`, because the year
    file is read with a plain `cache.get` that the warm refresh window
    does not reach, and a warmed row lives past its TTL.
    """
    last = today.replace(day=1) - timedelta(days=1)
    periods = {(today.year, today.month), (last.year, last.month)}
    years = {year for year, _ in periods}
    for year, month, page in _archive_files(session, inbox):
        if (year, month) in periods or (month is None and year in years):
            archive_sitemap_xml(session, inbox, year, month, page, base, force=True)


_INDEX_READ_BATCH = 16


def sitemap_indexes_xml(
    session: Session, base: str, *, force: bool = False
) -> tuple[SitemapPayload, SitemapPayload]:
    """Cached bodies of `/sitemap.xml` and `/messages-sitemap.xml` (#964),
    from one build so the slow `_month_counts` pass runs once for both.

    `/sitemap.xml` lists `/meta-sitemap.xml`, one `/<inbox>/sitemap.xml`
    per configured inbox with its archive `/t` files,
    `/sitemap-maintainers.xml`, and the sixteen series diff pages.
    `/messages-sitemap.xml` lists the message sibling of every archive
    file, same names with a `messages-` prefix, same page numbers: they
    are two halves of one build (`_split_by_kind`). Two independent
    indexes, neither referencing the other, is the shape Google accepts
    (see `_month_counts`). Same cache key as the routes use
    (`sitemap:indexsplit:<render-cap>`) so `warm-cache` can pre-populate
    it. The `last_modified` fields are the global-max article date. It
    is NOT projected into a `Last-Modified` response header: this is the
    surface whose date validator 304-pinned Cloudflare's edge on a
    pre-deploy index in 3.6.0, which is why no sitemap route sets one.
    See the module docstring."""

    def compute() -> tuple[SitemapPayload, SitemapPayload]:
        per_inbox_latest = _per_inbox_latest_date(session)
        global_latest = max((d for d in per_inbox_latest.values() if d), default=None)
        entries: list[tuple[str, str | None]] = [
            (f"{base}/meta-sitemap.xml", global_latest),
        ]
        message_entries: list[tuple[str, str | None]] = []
        inboxes = session.execute(select(Inbox).order_by(Inbox.name)).scalars().all()
        for inbox in inboxes:
            # The per-inbox file of hub pages.
            entries.append(
                (
                    f"{base}/{inbox.name}/sitemap.xml",
                    per_inbox_latest.get(inbox.name),
                )
            )
            # (directory, file name, key, is_year)
            children: list[tuple[str, str, str, bool]] = []
            for year, month, page in _archive_files(session, inbox):
                children.append(
                    (
                        f"{base}/{inbox.name}/{year:04d}/"
                        + ("" if month is None else f"{month:02d}/"),
                        "sitemap.xml" if page == 1 else f"sitemap-{page}.xml",
                        _year_pages_key(inbox.name, year)
                        if month is None
                        else _month_page_key(inbox.name, year, month, page),
                        month is None,
                    )
                )
            # lastmod = the child's cached last_modified; an uncached child
            # gets none (building it here would be a full scan). Same row, so
            # they agree while it lives, then can differ for one more TTL.
            # Batched: each cached child is a whole body.
            for start in range(0, len(children), _INDEX_READ_BATCH):
                batch = children[start : start + _INDEX_READ_BATCH]
                cached = cache.get_many([key for _, _, key, _ in batch])
                for directory, name, key, is_year in batch:
                    pair = cached.get(key)
                    if pair is not None and is_year:
                        pair = _unpack_year_pages(pair)[0]  # `name` is page 1
                    threads, singles = pair or (None, None)
                    entries.append(
                        (directory + name, threads and threads.last_modified)
                    )
                    message_entries.append(
                        (
                            f"{directory}messages-{name}",
                            singles and singles.last_modified,
                        )
                    )
        # Reuse global_latest as the maintainers urlset's lastmod: it's
        # free (already computed above) and a reasonable proxy for
        # "maintainer-relevant activity changed" without an extra
        # per-maintainer date query.
        entries.append((f"{base}/sitemap-maintainers.xml", global_latest))
        # No lastmod: dating a page would mean building it.
        entries.extend(
            (f"{base}/sitemap-series-diffs-{bucket}.xml", None)
            for bucket in SERIES_DIFF_BUCKETS
        )
        return (
            SitemapPayload(_build_sitemap_index_xml(entries), global_latest),
            SitemapPayload(_build_sitemap_index_xml(message_entries), global_latest),
        )

    return cache.get_or_compute(
        session,
        f"sitemap:indexsplit:{settings.thread_view_render_cap}",
        SITEMAP_TTL_SEC,
        compute,
        force=force,
    )


def meta_sitemap_xml(
    session: Session, base: str, *, force: bool = False
) -> SitemapPayload:
    """Cached body of `/meta-sitemap.xml`. Urlset covering `/`, with the
    global-max article date as lastmod, `/merge-window`, with the
    last linux-next walk, and `/llms.txt`, with no lastmod: it is a
    template whose text moves with deploys and robots rules, so no
    cheap date for it is truthful. The `last_modified`
    field carries the same global-max date so the route's
    body's `<lastmod>` stays truthful. It does not reach an HTTP
    `Last-Modified` header; sitemap routes set no validator at all (see
    the module docstring)."""

    def compute() -> SitemapPayload:
        # Late import: `mimir.merge_window` reaches the web layer's URL
        # helpers, which import this module's package at load time.
        from mimir.merge_window import MERGE_WINDOW_PATH, QUEUED_TREE
        from mimir.models import MainlineState

        per_inbox_latest = _per_inbox_latest_date(session)
        global_latest = max((d for d in per_inbox_latest.values() if d), default=None)
        # `/merge-window` changes when linux-next is walked.
        queued_walked_at = session.scalar(
            select(MainlineState.last_walked_at).where(
                MainlineState.tree_name == QUEUED_TREE
            )
        )
        return SitemapPayload(
            body=_build_sitemap_xml(
                [
                    SitemapEntry(base + "/", global_latest, PRIORITY_SITE),
                    SitemapEntry(
                        base + MERGE_WINDOW_PATH,
                        queued_walked_at.strftime("%Y-%m-%d")
                        if queued_walked_at
                        else None,
                        PRIORITY_SITE,
                    ),
                    SitemapEntry(base + LLMS_TXT_PATH, None, PRIORITY_SITE),
                ]
            ),
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
    every addressable maintainer profile (`/maintainers/<address>`).

    Slice-1 decision: no per-url `<lastmod>` and `last_modified=None`
    on the returned payload. (The route emits a plain 200 either way:
    no sitemap route sets a `Last-Modified` header, so a non-None value
    here would NOT produce one, contrary to what this said.)
    `all_maintainers` doesn't carry a
    per-maintainer "last active" date, and deriving one would mean a
    date query per maintainer (~4000 on the production MAINTAINERS
    corpus) just to populate a freshness hint crawlers treat as
    secondary to discovery. Revisit if maintainer profile pages ever
    gain a cheap last-changed signal."""

    def compute() -> SitemapPayload:
        entries = [
            SitemapEntry(base + maintainer_path(addr), None, PRIORITY_HUB)
            for addr, _name in all_maintainers(session)
            if is_addressable_maintainer_address(addr)
        ]
        return SitemapPayload(body=_build_sitemap_xml(entries), last_modified=None)

    return cache.get_or_compute(
        session,
        "sitemap:maintainers",
        SITEMAP_TTL_SEC,
        compute,
        force=force,
    )


# One urlset per first hex character of `patch_series_key` (a SHA-1 hex
# digest). Measured read-only on production 2026-10-24: 193,557 adjacent
# cover diffs, the largest bucket 12,478, so sixteen pages stay well under
# the 50,000 cap. A bucket is a range on `ix_articles_patch_series_key`,
# about 0.13 s warm (cold unmeasured), so pages are built on request and
# cached, like the archive pages, with no warm target.
SERIES_DIFF_BUCKETS = "0123456789abcdef"


def series_diff_sitemap_xml(
    session: Session, bucket: str, base: str, *, force: bool = False
) -> SitemapPayload:
    """Cached body of `/sitemap-series-diffs-<bucket>.xml` (#846): the
    cover-letter diff between each pair of adjacent revisions of every
    series whose key starts with `bucket`.

    Adjacent is `patch_series.adjacent_versions`, which the revisions
    fold links through too (#844): a series' covers oldest first, undated
    last, a resend folded into the first appearance of its version (an
    unversioned one is dropped).
    Covers only, because both covers must exist for the route to serve
    the diff, and per-position diffs would be 1.47M URLs (measured
    2026-10-24). `<lastmod>` is the newest cover in either slot: the
    mails are immutable, so only a resend moves it.
    """

    def compute() -> SitemapPayload:
        # Late import: a module-level one closes the `mimir.seo` <->
        # `mimir.web` cycle (see the package docstring and
        # `tests/test_import_graph.py`).
        from mimir.web.urls import _canonical_inbox_names_for, _spell_series_diff_url

        rows = session.execute(
            select(
                Article.id,
                Article.patch_series_key,
                series_head(),
                Article.patch_series_version,
                Article.date,
                article_has_files(),
            ).where(
                Article.patch_series_key >= bucket,
                Article.patch_series_key < chr(ord(bucket) + 1),
                func.coalesce(Article.patch_series_position, 0) == 0,
                # An unversioned `[PATCH RESEND 0/N]` copies a revision
                # already listed. Dropped rather than merged into v1: the
                # route's slots match the version string exactly, so its
                # date would move a lastmod the page's slots do not see.
                # SQL `!=` is never true for NULL, so this also drops covers
                # with no version, which the route cannot serve.
                Article.patch_series_version != "resend",
            )
        ).all()
        series: dict[tuple[str, int], list] = {}
        for art_id, key, head, version, date, diff in rows:
            series.setdefault((key, head), []).append((date, art_id, version, diff))

        pairs = []
        for (key, head), covers in sorted(series.items()):
            slot_dates: dict[str, list] = {}
            slot_ids: dict[str, list[tuple[int, bool]]] = {}
            for date, art_id, version, diff in covers:
                slot_dates.setdefault(version, []).append(date)
                slot_ids.setdefault(version, []).append((art_id, diff))
            # A version with no cover is absent here, so v1, v2 (no cover),
            # v3 pairs v1 with v3: adjacent among the revisions the route
            # can diff, and that page exists.
            for older, newer in adjacent_versions(covers):
                dates = [d for v in (older, newer) for d in slot_dates[v] if d]
                lastmod = max(dates).strftime("%Y-%m-%d") if dates else None
                to_id = min(slot_ids[newer], key=lambda r: slot_rank(*r))[0]
                pairs.append((key, head, older, newer, to_id, lastmod))

        # The page nominates the canonical inbox of the patch of its `to`
        # slot (`slot_rank`); this resolves the same rule for the whole
        # bucket.
        to_ids = [p[4] for p in pairs]
        inbox_of: dict[int, str] = {}
        for offset in range(0, len(to_ids), SITEMAP_URLS_PER_PAGE):
            inbox_of.update(
                _canonical_inbox_names_for(
                    session, to_ids[offset : offset + SITEMAP_URLS_PER_PAGE]
                )
            )
        entries = [
            SitemapEntry(
                _spell_series_diff_url(
                    inbox_of[to_id],
                    key,
                    older,
                    newer,
                    0,
                    head_id=head or None,
                    base=base,
                ),
                lastmod,
                PRIORITY_PAGE,
            )
            for key, head, older, newer, to_id, lastmod in pairs
            if to_id in inbox_of
        ]
        return SitemapPayload(
            body=_build_sitemap_xml(entries),
            last_modified=max((e.lastmod for e in entries if e.lastmod), default=None),
        )

    return cache.get_or_compute(
        session,
        f"sitemap:series-diffs:{bucket}",
        SITEMAP_TTL_SEC,
        compute,
        force=force,
    )


def inbox_sitemap_xml(
    session: Session, inbox: Inbox, base: str, *, force: bool = False
) -> SitemapPayload:
    """Cached body of `/<inbox>/sitemap.xml`: hub pages only. Dashboard +
    year/month archives that actually have data + the active subsystems
    and their index. Thread and message pages are in the archive files,
    which cover every root; a list of the most recent ones here only
    duplicated them and was dropped in #964. The `last_modified` field
    of the returned `SitemapPayload` is this inbox's most-recent
    article date, scoped so updates to other inboxes don't trigger
    Google to re-fetch this one."""

    def compute() -> SitemapPayload:
        entries: list[SitemapEntry] = []

        inbox_latest_dt = session.scalar(
            select(func.max(Article.date))
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date.is_not(None),
            )
        )
        inbox_latest = inbox_latest_dt.strftime("%Y-%m-%d") if inbox_latest_dt else None
        entries.append(
            SitemapEntry(f"{base}/{inbox.name}/", inbox_latest, PRIORITY_HUB)
        )

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
        # `inbox_latest`, the dashboard's own date, so the archives agree
        # with it.
        today = datetime.now(UTC).date().isoformat()
        for y in sorted(years_with_data, reverse=True):
            entries.append(
                SitemapEntry(
                    f"{base}/{inbox.name}/{y}/",
                    _archive_lastmod(int(y), None, today, inbox_latest),
                    PRIORITY_YEAR,
                )
            )
        for y, m in sorted(year_month_rows, reverse=True):
            entries.append(
                SitemapEntry(
                    f"{base}/{inbox.name}/{y}/{m}/",
                    _archive_lastmod(int(y), int(m), today, inbox_latest),
                    PRIORITY_MONTH,
                )
            )

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
        active = most_active_subsystems_in_inbox(
            session,
            inbox,
            days=7,
            limit=MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP,
            compute_on_miss=False,
        )
        for activity in active:
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
            # is `(loc, lastmod-as-str, ...)` and `_build_sitemap_xml` sets
            # the element text verbatim, so handing it a datetime is a
            # serialize-time TypeError, i.e. a 500 on the whole sitemap.
            entries.append(
                SitemapEntry(
                    loc,
                    (
                        activity.last_activity.strftime("%Y-%m-%d")
                        if activity.last_activity
                        else None
                    ),
                    PRIORITY_PAGE,
                )
            )
        # The index page, listed only when it is not empty. It renders a
        # bare "nothing active here" for the ~45-of-200 production
        # inboxes whose recent traffic touches no MAINTAINERS-claimed
        # path, and advertising those to crawlers is the same thin-page
        # trade this change set refuses everywhere else. It stays linked
        # from the inbox dashboard either way, so it is discoverable the
        # moment it has something to show. Dated by the newest activity
        # it lists, read from the same payload the page renders.
        if seen_subsystem_locs:
            newest_activity = max(
                (a.last_activity for a in active if a.last_activity), default=None
            )
            entries.append(
                SitemapEntry(
                    base + subsystem_index_path(inbox.name),
                    newest_activity.strftime("%Y-%m-%d") if newest_activity else None,
                    PRIORITY_PAGE,
                )
            )

        return SitemapPayload(
            body=_build_sitemap_xml(entries),
            last_modified=inbox_latest,
        )

    return cache.get_or_compute(
        session,
        # Hub pages only, so the render cap is not in the key (#964).
        f"sitemap:inboxhubs:{inbox.name}",
        SITEMAP_TTL_SEC,
        compute,
        force=force,
    )
