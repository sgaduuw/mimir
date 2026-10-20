"""Landing-page aggregations.

Helpers used by the `/` view to assemble its various surfaces.
Anything thread-shaped lives in `mimir.threading`; this module is for
the queries that aren't about thread reconstruction (trackers,
content-type filters, anniversaries, archive stats).

Every helper is cache-backed via `mimir.cache.get_or_compute`. The
underlying SQL involves LIKE / GLOB scans and is order-of-seconds on
a multi-million-row inbox; warming the cache (see `flask --app mimir
warm-cache`) keeps dashboard load times under cache-hit latency.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import DateTime, bindparam, select, text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.datetime_utils import coerce_dt
from mimir.models import Article, ArticleList, Inbox

STATS_CACHE_TTL_SEC = 86400  # 1 day
DAILY_VOLUME_CACHE_TTL_SEC = 3600  # 1 hour
LISTING_CACHE_TTL_SEC = 300  # 5 minutes, trackers, pulls, stable, history

# Recency floor for `latest_pull_requests` / `latest_stable_releases`.
# Without it, an inbox that has fewer than `limit` matches forces SQLite
# to walk the entire per-inbox date index proving the negative, which
# warm-cache was paying ~3 s per inbox for. Pull requests happen on
# every merge window (~8 10 weeks); stable releases are weekly. 180 d
# covers ~2 merge cycles and ~25 stable releases, comfortably above
# what the LIMIT-5 widgets surface. An inbox with no matches in this
# window renders an empty panel, which is the truthful answer.
LISTING_RECENCY_FLOOR_DAYS = 180

# Floor for the "first message" stat. Filters out the rare backdated repost
# (e.g. Linus's 1991 "hello minix" anniversary repost, which would otherwise
# make the archive's date-span footer claim mimir goes back to 1991). lkml
# itself didn't exist before ~1995.
STATS_MIN_PLAUSIBLE_DATE = "1995-01-01"


@dataclass
class ArticleSummary:
    """Compact view of an Article for cached listings (trackers, pull
    requests, stable releases, history). Carries just the fields
    templates and `_msg_url` need, so it round-trips through the
    cache without dragging the ORM along."""

    id: int
    subject: str | None
    author: str | None
    date: datetime | None


@dataclass
class ArchiveStats:
    total: int
    epochs: int
    first_date: datetime | None
    last_date: datetime | None


@dataclass
class DailyVolume:
    """Per-day message counts for the activity sparkline. `days` is
    zero-filled for any calendar day with no messages, so the bars line up
    on a uniform timeline."""

    days: list[tuple[date, int]]
    max_count: int


@dataclass
class MonthlyVolume:
    """Per-month message counts for the year archive view. `months`
    is zero-filled for every month in the year so the year grid
    always renders 12 cells."""

    year: int
    months: list[tuple[int, int]]  # (month_num 1..12, count)
    total: int


cache.register("ArticleSummary", ArticleSummary)
cache.register("ArchiveStats", ArchiveStats)
cache.register("DailyVolume", DailyVolume)
cache.register("MonthlyVolume", MonthlyVolume)


def _inbox_scoped(stmt, inbox: Inbox):
    """Restrict `stmt` to articles linked to `inbox`, via an EXISTS
    subquery rather than a JOIN.

    SQLite's planner mis-prices the parameterized `JOIN article_lists
    ON ... WHERE inbox_id = ?` shape and picks a scan-and-sort over
    the entire `article_lists` table, order-of-seconds on a multi-
    million-row inbox. The EXISTS form makes "walk articles in the
    relevant order, probe `article_lists` via composite PK" the
    natural plan, matching what literal binds would have produced.
    """
    return stmt.where(
        select(ArticleList.article_id)
        .where(
            ArticleList.article_id == Article.id,
            ArticleList.inbox_id == inbox.id,
        )
        .exists()
    )


def _summarize(articles: Sequence[Article]) -> list[ArticleSummary]:
    return [
        ArticleSummary(id=a.id, subject=a.subject, author=a.author, date=a.date)
        for a in articles
    ]


def author_recent(
    session: Session,
    inbox: Inbox,
    email_substring: str,
    limit: int = 5,
    force: bool = False,
) -> list[ArticleSummary]:
    """Last N messages in `inbox` whose From contains the substring.

    Always inbox-first: there is no date floor, so a date-first walk
    reads the whole archive for a sparse author (#796). The substring
    is not LIKE-escaped, as before; LIKE folds ASCII case as `ilike`
    did."""

    def compute() -> list[ArticleSummary]:
        return _inbox_first_rows(
            session,
            inbox,
            "a.author LIKE :pattern",
            {"pattern": f"%{email_substring}%"},
            limit,
        )

    return cache.get_or_compute(
        session,
        f"author_recent:{inbox.name}:{email_substring}:{limit}",
        LISTING_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def latest_pull_requests(
    session: Session, inbox: Inbox, limit: int = 5, force: bool = False
) -> list[ArticleSummary]:
    """Recent `[GIT PULL] ...` originals in `inbox`."""
    floor = datetime.now(UTC) - timedelta(days=LISTING_RECENCY_FLOOR_DAYS)

    def compute() -> list[ArticleSummary]:
        # LIKE treats `[` literally and folds ASCII case, as `ilike` did.
        if _inbox_first(inbox):
            return _inbox_first_listing(
                session, inbox, "a.subject LIKE '[GIT PULL]%'", floor, limit
            )
        rows = (
            session.execute(
                _inbox_scoped(
                    select(Article)
                    .where(Article.subject.ilike("[GIT PULL]%"))
                    .where(Article.date >= floor),
                    inbox,
                )
                .order_by(Article.date.desc().nulls_last(), Article.id.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return _summarize(rows)

    return cache.get_or_compute(
        session,
        f"latest_pull_requests:{inbox.name}:{limit}",
        LISTING_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def latest_stable_releases(
    session: Session, inbox: Inbox, limit: int = 5, force: bool = False
) -> list[ArticleSummary]:
    """Recent release announcements in `inbox`: subject starting with
    'Linux <digit>...'. GLOB is case-sensitive in SQLite."""
    floor = datetime.now(UTC) - timedelta(days=LISTING_RECENCY_FLOOR_DAYS)

    def compute() -> list[ArticleSummary]:
        if _inbox_first(inbox):
            return _inbox_first_listing(
                session, inbox, "a.subject GLOB 'Linux [0-9]*'", floor, limit
            )
        rows = (
            session.execute(
                _inbox_scoped(
                    select(Article)
                    .where(text("subject GLOB 'Linux [0-9]*'"))
                    .where(Article.date >= floor),
                    inbox,
                )
                .order_by(Article.date.desc().nulls_last(), Article.id.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return _summarize(rows)

    return cache.get_or_compute(
        session,
        f"latest_stable_releases:{inbox.name}:{limit}",
        LISTING_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def this_day_in_history(
    session: Session,
    inbox: Inbox,
    years_ago: int = 5,
    limit: int = 5,
    force: bool = False,
) -> list[ArticleSummary]:
    """A few messages from the same calendar day N years ago in `inbox`,
    most recent on that day first. (Was previously `ORDER BY RANDOM()`,
    which forced a full materialize of the day's rows.)"""
    # UTC date so the cache key advances at UTC midnight, matching the
    # compute window below (which derives `target` from `datetime.now(utc)`).
    # `date.today()` would advance at *local* midnight, so on a non-UTC
    # server the same request would hit a stale cache key for the wrong
    # day inside the offset window.
    today = datetime.now(UTC).date()

    def compute() -> list[ArticleSummary]:
        target = datetime.now(UTC) - timedelta(days=365 * years_ago)
        start = target.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        rows = (
            session.execute(
                _inbox_scoped(
                    select(Article).where(
                        Article.date >= start,
                        Article.date < end,
                    ),
                    inbox,
                )
                .order_by(Article.date.desc().nulls_last())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return _summarize(rows)

    return cache.get_or_compute(
        session,
        f"this_day_in_history:{inbox.name}:{years_ago}:{today.isoformat()}:{limit}",
        LISTING_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def daily_volume(
    session: Session, inbox: Inbox, days: int = 30, force: bool = False
) -> DailyVolume:
    """Daily message counts in `inbox` for the last `days` days,
    zero-filled. Cached per (inbox, days) key for 1 hour."""

    def compute() -> DailyVolume:
        # UTC date to match `Article.date`, which stores the public-inbox
        # commit time in UTC. `date.today()` would use the local date and
        # the window would slide by the server's UTC offset, slipping
        # edge messages in/out around UTC midnight.
        today = datetime.now(UTC).date()
        start = today - timedelta(days=days - 1)
        rows = session.execute(
            text(
                """
                SELECT date(a.date) AS day, COUNT(*) AS n
                FROM articles a
                JOIN article_lists al ON al.article_id = a.id
                WHERE al.inbox_id = :inbox_id AND a.date >= :start
                GROUP BY day
                """
            ),
            {"inbox_id": inbox.id, "start": start.isoformat()},
        ).all()
        counts = {date.fromisoformat(r.day): r.n for r in rows if r.day}
        series = [
            (start + timedelta(days=i), counts.get(start + timedelta(days=i), 0))
            for i in range(days)
        ]
        return DailyVolume(
            days=series,
            max_count=max((c for _, c in series), default=1),
        )

    return cache.get_or_compute(
        session,
        f"daily_volume:{inbox.name}:{days}",
        DAILY_VOLUME_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def like_escape(s: str) -> str:
    """Escape SQL-LIKE wildcards in a user-supplied substring so a
    query like `100%` doesn't quietly match every row. Pair with
    `escape="\\"` on the LIKE call (an `arch/x86_64/`-style value
    would widen unless `_` is escaped)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def recent_page(
    session: Session, inbox: Inbox, offset: int, limit: int
) -> list[ArticleSummary]:
    """`limit` newest articles of `inbox` from `offset`. Inbox-first
    for lists under `RECENT_INBOX_FIRST_BELOW_ARTICLES`: a dormant one
    otherwise walks the date index back to its last message (#796).
    Unlike `_inbox_first`, an unknown size takes the date walk: this
    listing is uncached, and guessing small would sort all of lkml on
    the inbox page and on every Load more."""
    total = cached_archive_total(inbox)
    if total is not None and total < RECENT_INBOX_FIRST_BELOW_ARTICLES:
        return _inbox_first_rows(session, inbox, "1", {}, limit, offset)
    rows = (
        session.execute(
            _inbox_scoped(select(Article), inbox)
            .order_by(Article.date.desc().nulls_last(), Article.id.desc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return _summarize(rows)


def recent_articles(
    session: Session,
    inbox: Inbox,
    limit: int = 50,
    force: bool = False,
) -> list[ArticleSummary]:
    """Most-recent `limit` articles in `inbox`, ordered by date desc.
    Cached at the listing TTL, feeds polling on 30 min+ intervals
    always pay zero compute, since warm-cache keeps it hot.

    No offset, pagination lives in `web._fetch_recent` which serves
    the dashboard's "Load more" pattern; this helper is the
    feed/Atom data source.
    """

    def compute() -> list[ArticleSummary]:
        return recent_page(session, inbox, 0, limit)

    return cache.get_or_compute(
        session,
        f"recent_articles:{inbox.name}:{limit}",
        LISTING_CACHE_TTL_SEC,
        compute,
        force=force,
    )


# Inbox-first: seek this inbox's `article_lists` rows, fetch each article
# by primary key. CROSS JOIN pins that order (SQLite never reorders it),
# as in `_triage_sql`; INDEXED BY pins the seek.
def _inbox_first_sql(where: str) -> str:
    """The inbox-first SELECT for a WHERE fragment over alias `a`."""
    return f"""
    SELECT a.id, a.subject, a.author, a.date
    FROM article_lists al INDEXED BY ix_article_lists_inbox_id
         CROSS JOIN articles a ON a.id = al.article_id
    WHERE al.inbox_id = :inbox_id
      AND {where}
    -- id DESC breaks date ties the way the old date-index walk did,
    -- so LIMIT keeps the same rows at a tie.
    ORDER BY a.date DESC NULLS LAST, a.id DESC
    LIMIT :limit OFFSET :offset
"""


# `\\` is the LIKE escape produced by `like_escape`.
SEARCH_WHERE = r"""(a.subject LIKE :pattern ESCAPE '\'
           OR a.author LIKE :pattern ESCAPE '\')"""


def _inbox_first_rows(
    session: Session,
    inbox: Inbox,
    where: str,
    binds: dict,
    limit: int,
    offset: int = 0,
) -> list[ArticleSummary]:
    """Run the inbox-first statement for `where`; `binds` carries its
    parameters, with `floor` (a datetime) bound as a DateTime."""
    stmt = text(_inbox_first_sql(where))
    if "floor" in binds:
        stmt = stmt.bindparams(bindparam("floor", type_=DateTime))
    rows = session.execute(
        stmt, {**binds, "inbox_id": inbox.id, "limit": limit, "offset": offset}
    )
    return [
        ArticleSummary(
            id=r.id, subject=r.subject, author=r.author, date=coerce_dt(r.date)
        )
        for r in rows
    ]


def _inbox_first_listing(
    session: Session, inbox: Inbox, where: str, floor: datetime, limit: int
) -> list[ArticleSummary]:
    """`where` matches over the last `LISTING_RECENCY_FLOOR_DAYS`,
    newest first, read inbox-first."""
    return _inbox_first_rows(
        session, inbox, f"{where} AND a.date >= :floor", {"floor": floor}, limit
    )


def search_articles(
    session: Session,
    inbox: Inbox,
    query: str,
    limit: int = 100,
    force: bool = False,
) -> list[ArticleSummary]:
    """Substring search over `subject` and `author` in `inbox`,
    case-insensitive, ordered by date desc (undated last), capped at
    `limit`.

    Reads inbox-first (`_inbox_first_sql`), so the cost is bounded by the
    list's size. A date-first walk stops early only once it has `limit`
    matches; with fewer (a small list, or a rare term) it reads the
    whole archive (#791).

    Cached per (inbox, query.lower(), limit) at the listing TTL. The
    cache key is case-folded so `Foo` and `foo` share one row, as
    SQLite's LIKE folds ASCII case. The caller is
    responsible for length-bounding `query` and for asking only when
    the user typed something meaningful (>=2 chars).
    """

    def compute() -> list[ArticleSummary]:
        return _inbox_first_rows(
            session,
            inbox,
            SEARCH_WHERE,
            {"pattern": f"%{like_escape(query)}%"},
            limit,
        )

    return cache.get_or_compute(
        session,
        f"search:{inbox.name}:{query.lower()}:{limit}",
        LISTING_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def monthly_volume(
    session: Session, inbox: Inbox, year: int, force: bool = False
) -> MonthlyVolume:
    """Per-month counts for `year` in `inbox`, zero-filled. Cached
    per (inbox, year) for 1 hour, current-year counts evolve, past
    years are immutable.

    Past years could in principle be cached forever, but the constant
    1-hour TTL keeps the helper composable: warming and invalidating
    are uniform across years.
    """

    def compute() -> MonthlyVolume:
        rows = session.execute(
            text(
                """
                SELECT CAST(strftime('%m', a.date) AS INTEGER) AS month,
                       COUNT(*) AS n
                FROM articles a
                WHERE EXISTS (
                    SELECT 1 FROM article_lists al
                    WHERE al.article_id = a.id AND al.inbox_id = :inbox_id
                )
                  AND a.date >= :year_start
                  AND a.date <  :year_end
                GROUP BY month
                """
            ),
            {
                "inbox_id": inbox.id,
                "year_start": f"{year:04d}-01-01 00:00:00",
                "year_end": f"{year + 1:04d}-01-01 00:00:00",
            },
        ).all()
        counts = {r.month: r.n for r in rows if r.month is not None}
        months = [(m, counts.get(m, 0)) for m in range(1, 13)]
        return MonthlyVolume(
            year=year,
            months=months,
            total=sum(c for _, c in months),
        )

    return cache.get_or_compute(
        session,
        f"monthly_volume:{inbox.name}:{year}",
        DAILY_VOLUME_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def _archive_stats_key(inbox: Inbox) -> str:
    return f"archive_stats:{inbox.name}"


def cached_archive_total(inbox: Inbox) -> int | None:
    """The inbox's article count from the cached `archive_stats` row,
    or None when there is no live row. Never computes the COUNT(*)
    (the ingest tick warms the row, and a caller that only wants a
    size hint must not pay for a miss)."""
    stats = cache.get(_archive_stats_key(inbox))
    return None if stats is None else stats.total


# Inboxes with at least this many articles (all time, `archive_stats`)
# drive their listing queries from `ix_articles_date`; smaller ones from
# their own `article_lists` rows. Users: the triage queues (#775) and
# the pull-request and stable-release listings (#796) through
# `_inbox_first`; the unfiltered recent listing has its own, lower
# threshold below. Measured on production on 2026-10-18,
# the same rows from both shapes, first page, in seconds
# (date-first vs inbox-first): lkml (6.58M articles) 0.60 vs 5.58;
# netdev (1.28M) 0.10 vs 1.58 on a dense subsystem; u-boot (634k)
# 14.02 vs 0.49; linux-fsdevel (368k) 13.7 vs 0.35; alsa-devel (284k)
# 14.06 vs 0.28. The split falls between u-boot and netdev, so 1M. A
# sparse netdev subsystem still takes the slow date-first walk (14.09
# vs 1.72); that is the cost of choosing per inbox, not per subsystem.
INBOX_FIRST_BELOW_ARTICLES = 1_000_000

# The same switch for `recent_page` (unfiltered, so every article
# matches and the date walk stops after one page on a busy list).
# Measured on production, date-first vs inbox-first, in ms at offsets
# 0 / 2550: u-boot (634k) 2 / 49 vs 301 / 562; linux-fsdevel (368k)
# 1 / 43 vs 147 / 318. Below 100k inbox-first is bounded at tens of
# ms, and a dormant list needs it: stgt 3712 / 6273 vs 0 / 0;
# kernelnewbies 370 / 2643 vs 11 / 21; keyrings 1 / 683 vs 17 / 26.
# A dormant list above 100k stays as slow as before; accepted.
RECENT_INBOX_FIRST_BELOW_ARTICLES = 100_000


def _inbox_first(inbox: Inbox) -> bool:
    """Whether to drive from the inbox's own rows: the inbox has fewer
    than `INBOX_FIRST_BELOW_ARTICLES` articles. An unknown size (no live `archive_stats`
    row) counts as small: inbox-first's worst measured case (5.85 s,
    the biggest list) beats date-first's (14 s, a small list)."""
    total = cached_archive_total(inbox)
    return total is None or total < INBOX_FIRST_BELOW_ARTICLES


def archive_stats(session: Session, inbox: Inbox, force: bool = False) -> ArchiveStats:
    """Total row count + date span + epoch count for `inbox`. Cached
    per-inbox for 24h. COUNT(*) over a single inbox still does a scan but
    is cheaper than across all inboxes.

    `last_date` is overlaid from `Inbox.last_article_date` (bumped on
    every ingest commit, see #216) instead of being part of the
    cached row, so the front-page "Last activity" string doesn't ride
    the 24h cache window. The COUNT(*) is the slow piece that
    justifies the TTL; first_date / epochs drift on geological
    timescales by comparison.
    """

    def compute() -> ArchiveStats:
        row = session.execute(
            text(
                """
                SELECT COUNT(*) AS total,
                       MIN(a.date) FILTER (WHERE a.date >= :min_date) AS first_date,
                       COUNT(DISTINCT al.epoch) AS epochs
                FROM articles a
                JOIN article_lists al ON al.article_id = a.id
                WHERE al.inbox_id = :inbox_id
                """
            ),
            {"inbox_id": inbox.id, "min_date": STATS_MIN_PLAUSIBLE_DATE},
        ).one()
        return ArchiveStats(
            total=row.total,
            epochs=row.epochs,
            first_date=coerce_dt(row.first_date),
            last_date=None,
        )

    cached = cache.get_or_compute(
        session,
        _archive_stats_key(inbox),
        STATS_CACHE_TTL_SEC,
        compute,
        force=force,
    )
    return ArchiveStats(
        total=cached.total,
        epochs=cached.epochs,
        first_date=cached.first_date,
        last_date=inbox.last_article_date,
    )
