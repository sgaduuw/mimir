"""Calendar-shaped views: today / yesterday / since / year / month
archives.

Per-epoch listings were rejected (epoch numbers are a public-inbox
storage chunking artifact with no semantic meaning to a reader);
date-scoped views are the right shape for "what happened on a day".
"""

from datetime import date as date_cls, datetime, timedelta, timezone

from flask import abort, render_template
from sqlalchemy import func, select

from mimir.dashboard import monthly_volume
from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList
from mimir.threading import (
    THREADS_SINCE_MAX_DAYS,
    threads_for_day,
    threads_for_month,
    threads_since,
)
from mimir.web._blueprint import bp_web
from mimir.web.urls import _get_inbox_or_404


# Plausible bounds for an inbox archive: lkml itself goes back to ~1995.
# Outside this range a year URL is almost certainly user error / scraper
# noise; 404 is the right response.
_MIN_ARCHIVE_YEAR = 1995

# Cap for the month-archive thread list. A busy month on lkml has
# thousands of threads; rendering all of them blows the response past
# 1 MB. The view shows the most-recent N + the total-message count
# from `monthly_volume` so context isn't lost.
MONTH_THREAD_CAP = 100


def _max_archive_year() -> int:
    return datetime.now(timezone.utc).year + 1


def _daily_view(inbox_name: str, day: date_cls, heading: str):
    """Shared renderer for /<list>/today and /<list>/yesterday."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        threads = threads_for_day(session, inbox, day)
        # Compare against datetime values (matching the
        # `dashboard.py` pattern) rather than `strftime` strings.
        # SQLAlchemy 2.x routes datetime bind params through the
        # column's DateTime type; the strftime form bypasses that
        # and drops tz info on a column that's been documented as
        # tz-aware UTC.
        total = session.scalar(
            select(func.count(Article.id))
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date >= start,
                Article.date < end,
            )
        )
    return render_template(
        "daily.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        day=day,
        heading=heading,
        threads=threads,
        total_messages=total or 0,
    )


@bp_web.route("/<inbox_name>/today")
def daily_today(inbox_name: str):
    today = datetime.now(timezone.utc).date()
    return _daily_view(inbox_name, today, "Today")


@bp_web.route("/<inbox_name>/yesterday")
def daily_yesterday(inbox_name: str):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    return _daily_view(inbox_name, yesterday, "Yesterday")


@bp_web.route("/<inbox_name>/since/<since_str>")
def threads_since_view(inbox_name: str, since_str: str):
    """ "What I missed" view: every thread with activity from `since` to
    now. Window is clamped to `THREADS_SINCE_MAX_DAYS` (90 days) below
    the present; the template renders a notice when the requested
    `since` falls before the cap so the operator sees why the window
    starts where it does."""
    try:
        since = date_cls.fromisoformat(since_str)
    except ValueError:
        abort(404)
    today = datetime.now(timezone.utc).date()
    if since > today:
        abort(404)
    floor = today - timedelta(days=THREADS_SINCE_MAX_DAYS)
    capped = since < floor
    effective = floor if capped else since
    start = datetime.combine(effective, datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.now(timezone.utc)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        threads = threads_since(session, inbox, since)
        # Same datetime-not-strftime treatment as `_daily_view`.
        total = session.scalar(
            select(func.count(Article.id))
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date >= start,
                Article.date < end,
            )
        )
    return render_template(
        "since.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        since=since,
        effective_since=effective,
        capped=capped,
        max_days=THREADS_SINCE_MAX_DAYS,
        threads=threads,
        total_messages=total or 0,
    )


@bp_web.route("/<inbox_name>/<int:year>/")
def year_archive(inbox_name: str, year: int):
    """Year view for an inbox: 12 month cells with per-month message
    counts, cells link to the month view."""
    if year < _MIN_ARCHIVE_YEAR or year > _max_archive_year():
        abort(404)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        volume = monthly_volume(session, inbox, year)
    return render_template(
        "year.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        year=year,
        volume=volume,
        prev_year=year - 1 if year - 1 >= _MIN_ARCHIVE_YEAR else None,
        next_year=year + 1 if year + 1 <= _max_archive_year() else None,
    )


@bp_web.route("/<inbox_name>/<int:year>/<int:month>/")
def month_archive(inbox_name: str, year: int, month: int):
    """Month view for an inbox: every thread with at least one
    message in (year, month), ordered by last activity desc."""
    if year < _MIN_ARCHIVE_YEAR or year > _max_archive_year():
        abort(404)
    if month < 1 or month > 12:
        abort(404)
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        threads = threads_for_month(session, inbox, year, month, limit=MONTH_THREAD_CAP)
        # Reuse the cached `monthly_volume` count, keeps the warm-
        # path off the COUNT(*) over the month's article rows.
        volume = monthly_volume(session, inbox, year)
        total = next((c for m, c in volume.months if m == month), 0)

    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    return render_template(
        "month.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        year=year,
        month=month,
        month_label=start.strftime("%B %Y"),
        threads=threads,
        total_messages=total or 0,
        thread_cap=MONTH_THREAD_CAP,
        prev_url=(
            f"/{inbox.name}/{prev_year:04d}/{prev_month:02d}/"
            if prev_year >= _MIN_ARCHIVE_YEAR
            else None
        ),
        next_url=(
            f"/{inbox.name}/{next_year:04d}/{next_month:02d}/"
            if next_year <= _max_archive_year()
            else None
        ),
    )
