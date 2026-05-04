"""Landing-page aggregations.

Helpers used by the `/` view to assemble its various surfaces.
Anything thread-shaped lives in `mimir.threading`; this module is for
the queries that aren't about thread reconstruction (trackers,
content-type filters, anniversaries, archive stats).
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.models import Article, ArticleList, Inbox
from mimir.threading import _coerce_dt

STATS_CACHE_TTL_SEC = 86400  # 1 day
DAILY_VOLUME_CACHE_TTL_SEC = 3600  # 1 hour

# Floor for the "first message" stat. Filters out the rare backdated repost
# (e.g. Linus's 1991 "hello minix" anniversary repost, which would otherwise
# make the archive's date-span footer claim mimir goes back to 1991). lkml
# itself didn't exist before ~1995.
STATS_MIN_PLAUSIBLE_DATE = "1995-01-01"


def _inbox_scoped(stmt, inbox: Inbox):
    """Add the article_lists join + inbox filter to a select(Article)."""
    return stmt.join(ArticleList, ArticleList.article_id == Article.id).where(
        ArticleList.inbox_id == inbox.id
    )


def author_recent(
    session: Session, inbox: Inbox, email_substring: str, limit: int = 5
) -> Sequence[Article]:
    """Last N messages in `inbox` whose From contains the substring."""
    return session.execute(
        _inbox_scoped(
            select(Article).where(Article.author.ilike(f"%{email_substring}%")),
            inbox,
        )
        .order_by(Article.date.desc().nulls_last())
        .limit(limit)
    ).scalars().all()


def latest_pull_requests(
    session: Session, inbox: Inbox, limit: int = 5
) -> Sequence[Article]:
    """Recent `[GIT PULL] ...` originals in `inbox`."""
    return session.execute(
        _inbox_scoped(
            select(Article).where(Article.subject.ilike("[GIT PULL]%")),
            inbox,
        )
        .order_by(Article.date.desc().nulls_last())
        .limit(limit)
    ).scalars().all()


def latest_stable_releases(
    session: Session, inbox: Inbox, limit: int = 5
) -> Sequence[Article]:
    """Recent release announcements in `inbox`: subject starting with
    'Linux <digit>...'. GLOB is case-sensitive in SQLite."""
    return session.execute(
        _inbox_scoped(
            select(Article).where(text("subject GLOB 'Linux [0-9]*'")),
            inbox,
        )
        .order_by(Article.date.desc().nulls_last())
        .limit(limit)
    ).scalars().all()


def this_day_in_history(
    session: Session, inbox: Inbox, years_ago: int = 5, limit: int = 5
) -> Sequence[Article]:
    """A few messages from the same calendar day N years ago in `inbox`."""
    now = datetime.now(timezone.utc)
    target = now - timedelta(days=365 * years_ago)
    start = target.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return session.execute(
        _inbox_scoped(
            select(Article).where(
                Article.date >= start,
                Article.date < end,
            ),
            inbox,
        )
        .order_by(text("RANDOM()"))
        .limit(limit)
    ).scalars().all()


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


cache.register("ArchiveStats", ArchiveStats)
cache.register("DailyVolume", DailyVolume)


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
