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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.models import Article, ArticleList, Inbox
from mimir.threading import _coerce_dt

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
    """Last N messages in `inbox` whose From contains the substring."""
    def compute() -> list[ArticleSummary]:
        rows = session.execute(
            _inbox_scoped(
                select(Article).where(Article.author.ilike(f"%{email_substring}%")),
                inbox,
            )
            .order_by(Article.date.desc().nulls_last())
            .limit(limit)
        ).scalars().all()
        return _summarize(rows)

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
    floor = datetime.now(timezone.utc) - timedelta(days=LISTING_RECENCY_FLOOR_DAYS)

    def compute() -> list[ArticleSummary]:
        rows = session.execute(
            _inbox_scoped(
                select(Article)
                .where(Article.subject.ilike("[GIT PULL]%"))
                .where(Article.date >= floor),
                inbox,
            )
            .order_by(Article.date.desc().nulls_last())
            .limit(limit)
        ).scalars().all()
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
    floor = datetime.now(timezone.utc) - timedelta(days=LISTING_RECENCY_FLOOR_DAYS)

    def compute() -> list[ArticleSummary]:
        rows = session.execute(
            _inbox_scoped(
                select(Article)
                .where(text("subject GLOB 'Linux [0-9]*'"))
                .where(Article.date >= floor),
                inbox,
            )
            .order_by(Article.date.desc().nulls_last())
            .limit(limit)
        ).scalars().all()
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
    today = date.today()

    def compute() -> list[ArticleSummary]:
        target = datetime.now(timezone.utc) - timedelta(days=365 * years_ago)
        start = target.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        rows = session.execute(
            _inbox_scoped(
                select(Article).where(
                    Article.date >= start,
                    Article.date < end,
                ),
                inbox,
            )
            .order_by(Article.date.desc().nulls_last())
            .limit(limit)
        ).scalars().all()
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
        today = date.today()
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
    `escape="\\"` on the LIKE call. Also used by `mimir.subsystems`
    on MAINTAINERS-rule globs (an `arch/x86_64/` directory would
    widen unless `_` is escaped)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
        rows = session.execute(
            _inbox_scoped(select(Article), inbox)
            .order_by(Article.date.desc().nulls_last())
            .limit(limit)
        ).scalars().all()
        return _summarize(rows)

    return cache.get_or_compute(
        session,
        f"recent_articles:{inbox.name}:{limit}",
        LISTING_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def search_articles(
    session: Session,
    inbox: Inbox,
    query: str,
    limit: int = 100,
    force: bool = False,
) -> list[ArticleSummary]:
    """Substring search over `subject` and `author` in `inbox`,
    case-insensitive, ordered by date desc, capped at `limit`.

    Implementation is a straight LIKE scan with the date index
    short-circuiting; cached per (inbox, query.lower(), limit) at the
    listing TTL. The cache key is case-folded so `Foo` and `foo`
    share one row, mirroring `ilike()`'s case-insensitivity — the
    SQL would return identical results either way. The caller is
    responsible for length-bounding `query` and for asking only
    when the user typed something meaningful (≥2 chars).
    """
    def compute() -> list[ArticleSummary]:
        pattern = f"%{like_escape(query)}%"
        rows = session.execute(
            _inbox_scoped(
                select(Article).where(
                    or_(
                        Article.subject.ilike(pattern, escape="\\"),
                        Article.author.ilike(pattern, escape="\\"),
                    )
                ),
                inbox,
            )
            .order_by(Article.date.desc().nulls_last())
            .limit(limit)
        ).scalars().all()
        return _summarize(rows)

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
                "year_end":   f"{year + 1:04d}-01-01 00:00:00",
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


def archive_stats(
    session: Session, inbox: Inbox, force: bool = False
) -> ArchiveStats:
    """Total row count + date span + epoch count for `inbox`. Cached
    per-inbox for 24h. COUNT(*) over a single inbox still does a scan but
    is cheaper than across all inboxes."""
    def compute() -> ArchiveStats:
        row = session.execute(
            text(
                """
                SELECT COUNT(*) AS total,
                       MIN(a.date) FILTER (WHERE a.date >= :min_date) AS first_date,
                       MAX(a.date) AS last_date,
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
            first_date=_coerce_dt(row.first_date),
            last_date=_coerce_dt(row.last_date),
        )

    return cache.get_or_compute(
        session,
        f"archive_stats:{inbox.name}",
        STATS_CACHE_TTL_SEC,
        compute,
        force=force,
    )
