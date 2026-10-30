"""Calendar-shaped views: today / yesterday / since / year / month
archives.

Per-epoch listings were rejected (epoch numbers are a public-inbox
storage chunking artifact with no semantic meaning to a reader);
date-scoped views are the right shape for "what happened on a day".
"""

from datetime import UTC, datetime, timedelta
from datetime import date as date_cls

from flask import abort, render_template

from mimir.dashboard import messages_in_window, monthly_volume
from mimir.extensions import SessionLocal
from mimir.lifecycle_status import lifecycle_status_for_articles
from mimir.threading import (
    THREADS_SINCE_MAX_DAYS,
    THREADS_SINCE_MAX_THREADS,
    since_window_start,
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
    return datetime.now(UTC).year + 1


def _daily_view(inbox_name: str, day: date_cls, heading: str):
    """Shared renderer for /<list>/today and /<list>/yesterday."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        threads = threads_for_day(session, inbox, day)
        total = messages_in_window(session, inbox, start, end)
        lifecycle_status_by_id = lifecycle_status_for_articles(
            session, [t.id for t in threads], inbox.id
        )
    return render_template(
        "daily.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        day=day,
        heading=heading,
        threads=threads,
        total_messages=total,
        lifecycle_status_by_id=lifecycle_status_by_id,
    )


@bp_web.route("/<inbox_name>/today")
def daily_today(inbox_name: str):
    today = datetime.now(UTC).date()
    return _daily_view(inbox_name, today, "Today")


@bp_web.route("/<inbox_name>/yesterday")
def daily_yesterday(inbox_name: str):
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    return _daily_view(inbox_name, yesterday, "Yesterday")


@bp_web.route("/<inbox_name>/since/<since_str>")
def threads_since_view(inbox_name: str, since_str: str):
    """ "What I missed" view: the most recently active threads (up to
    `THREADS_SINCE_MAX_THREADS`) with activity from `since` to now.
    Window is clamped to `THREADS_SINCE_MAX_DAYS` (90 days) below
    the present; the template renders a notice when the requested
    `since` falls before the cap so the operator sees why the window
    starts where it does."""
    try:
        since = date_cls.fromisoformat(since_str)
    except ValueError:
        abort(404)
    today = datetime.now(UTC).date()
    if since > today:
        abort(404)
    floor = today - timedelta(days=THREADS_SINCE_MAX_DAYS)
    capped = since < floor
    effective = floor if capped else since
    end = datetime.now(UTC)
    start = since_window_start(effective, end)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        # `effective`, not `since`: every pre-floor date is the same
        # window, so it should be one cache key and one compute.
        threads = threads_since(session, inbox, effective)
        truncated = len(threads) > THREADS_SINCE_MAX_THREADS
        threads = threads[:THREADS_SINCE_MAX_THREADS]
        total = messages_in_window(session, inbox, start, end)
        lifecycle_status_by_id = lifecycle_status_for_articles(
            session, [t.id for t in threads], inbox.id
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
        truncated=truncated,
        max_threads=THREADS_SINCE_MAX_THREADS,
        total_messages=total,
        lifecycle_status_by_id=lifecycle_status_by_id,
        # "What I missed" is a personal catch-up window, not archival
        # content: every date in the 90-day cap is a distinct URL over
        # heavily overlapping thread sets, so indexing them would be
        # ~90 near-duplicate pages per inbox competing with the thread
        # pages they link to. `noindex` implies follow (only an
        # explicit `nofollow` disables it), so the outbound links stay
        # a crawl path into real content. Not robots-disallowed like
        # `/api/` and `/*/search`: no template links here, so there is
        # no crawl budget to reclaim, and a shared link should still
        # render and pass equity onward.
        noindex=True,
    )


@bp_web.route("/<inbox_name>/<int:year>/")
def year_archive(inbox_name: str, year: int):
    """Year view for an inbox: 12 month rows with a per-month message
    count and a bar scaled to the busiest month. Rows with messages
    link to the month view."""
    if year < _MIN_ARCHIVE_YEAR or year > _max_archive_year():
        abort(404)
    now = datetime.now(UTC)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        volume = monthly_volume(session, inbox, year)
        # An empty next year is a dead end, so only link forward when
        # there is something there. `monthly_volume` is cached per year.
        has_next = year + 1 <= _max_archive_year() and (
            monthly_volume(session, inbox, year + 1).total > 0
        )
    return render_template(
        "year.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        year=year,
        volume=volume,
        prev_year=year - 1 if year - 1 >= _MIN_ARCHIVE_YEAR else None,
        next_year=year + 1 if has_next else None,
        is_current_year=year == now.year,
        current_month=now.month,
    )


@bp_web.route("/<inbox_name>/<int:year>/<int:month>/")
def month_archive(inbox_name: str, year: int, month: int):
    """Month view for an inbox: every thread with at least one
    message in (year, month), ordered by last activity desc."""
    if year < _MIN_ARCHIVE_YEAR or year > _max_archive_year():
        abort(404)
    if month < 1 or month > 12:
        abort(404)
    start = datetime(year, month, 1, tzinfo=UTC)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        threads = threads_for_month(session, inbox, year, month, limit=MONTH_THREAD_CAP)
        # Reuse the cached `monthly_volume` count, keeps the warm-
        # path off the COUNT(*) over the month's article rows.
        volume = monthly_volume(session, inbox, year)
        total = next((c for m, c in volume.months if m == month), 0)
        lifecycle_status_by_id = lifecycle_status_for_articles(
            session, [t.id for t in threads], inbox.id
        )

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
        lifecycle_status_by_id=lifecycle_status_by_id,
    )
