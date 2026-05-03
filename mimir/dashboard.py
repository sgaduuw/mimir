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
from mimir.models import Article
from mimir.threading import _coerce_dt

STATS_CACHE_TTL_SEC = 86400  # 1 day
DAILY_VOLUME_CACHE_TTL_SEC = 3600  # 1 hour

# Floor for the "first message" stat. Filters out the rare backdated repost
# (e.g. Linus's 1991 "hello minix" anniversary repost, which would otherwise
# make the archive's date-span footer claim mimir goes back to 1991). lkml
# itself didn't exist before ~1995.
STATS_MIN_PLAUSIBLE_DATE = "1995-01-01"


def author_recent(
    session: Session, email_substring: str, limit: int = 5
) -> Sequence[Article]:
    """Last N messages whose From address contains the given substring."""
    return session.execute(
        select(Article)
        .where(Article.author.ilike(f"%{email_substring}%"))
        .order_by(Article.date.desc().nulls_last())
        .limit(limit)
    ).scalars().all()


def latest_pull_requests(session: Session, limit: int = 5) -> Sequence[Article]:
    """Recent `[GIT PULL] ...` originals (Re: replies excluded — those are
    Linus's merge confirmations, not the actual pulls)."""
    return session.execute(
        select(Article)
        .where(Article.subject.ilike("[GIT PULL]%"))
        .order_by(Article.date.desc().nulls_last())
        .limit(limit)
    ).scalars().all()


def latest_stable_releases(session: Session, limit: int = 5) -> Sequence[Article]:
    """Recent release announcements: subject starting with 'Linux <digit>...',
    e.g. 'Linux 6.13-rc5' from Linus or 'Linux 6.12.4' from Greg."""
    # GLOB is case-sensitive in SQLite; '[0-9]' is a character class.
    return session.execute(
        select(Article)
        .where(text("subject GLOB 'Linux [0-9]*'"))
        .order_by(Article.date.desc().nulls_last())
        .limit(limit)
    ).scalars().all()


def this_day_in_history(
    session: Session, years_ago: int = 5, limit: int = 5
) -> Sequence[Article]:
    """A few messages from the same calendar day N years ago (default 5).
    Random sample within that day for variety on each page load."""
    now = datetime.now(timezone.utc)
    target = now - timedelta(days=365 * years_ago)
    start = target.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return session.execute(
        select(Article)
        .where(Article.date >= start, Article.date < end)
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


def daily_volume(
    session: Session, days: int = 30, force: bool = False
) -> DailyVolume:
    """Daily message counts for the last `days` days, zero-filled.
    GROUP BY date(date) does the bucketing; the date index keeps the
    underlying scan to the recent slice. ~100-200 ms uncached;
    cached on disk for DAILY_VOLUME_CACHE_TTL_SEC (1h)."""
    cache_key = f"daily_volume:{days}"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    today = date.today()
    start = today - timedelta(days=days - 1)
    rows = session.execute(
        text(
            """
            SELECT date(date) AS day, COUNT(*) AS n
            FROM articles
            WHERE date >= :start
            GROUP BY day
            """
        ),
        {"start": start.isoformat()},
    ).all()
    counts = {date.fromisoformat(r.day): r.n for r in rows if r.day}
    series = [
        (start + timedelta(days=i), counts.get(start + timedelta(days=i), 0))
        for i in range(days)
    ]
    result = DailyVolume(
        days=series,
        max_count=max((c for _, c in series), default=1),
    )
    cache.set(cache_key, result, ttl=DAILY_VOLUME_CACHE_TTL_SEC)
    return result


def archive_stats(session: Session, force: bool = False) -> ArchiveStats:
    """Total row count + date span + epoch count, cached on disk for 24h.
    COUNT(*) is the slow piece (~6 s on 6.2M rows); MIN/MAX use the date
    index, COUNT(DISTINCT epoch) hits the epoch index. Pass force=True
    to bypass the cache (used by the `warm-cache` CLI)."""
    cache_key = "archive_stats"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    row = session.execute(
        text(
            """
            SELECT COUNT(*) AS total,
                   MIN(date) FILTER (WHERE date >= :min_date) AS first_date,
                   MAX(date) AS last_date,
                   COUNT(DISTINCT epoch) AS epochs
            FROM articles
            """
        ),
        {"min_date": STATS_MIN_PLAUSIBLE_DATE},
    ).one()
    stats = ArchiveStats(
        total=row.total,
        epochs=row.epochs,
        first_date=_coerce_dt(row.first_date),
        last_date=_coerce_dt(row.last_date),
    )
    cache.set(cache_key, stats, ttl=STATS_CACHE_TTL_SEC)
    return stats
