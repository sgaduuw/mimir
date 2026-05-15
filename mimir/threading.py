"""Thread reconstruction for the message view.

Uses SQLite recursive CTEs against the `thread_parent` graph (best-guess
parent: In-Reply-To OR last entry of References, computed at ingest):
- `find_thread_root` walks up to the topmost ancestor that's actually in
  our DB. A message whose parent isn't in the archive is its own root
  (we don't show phantom containers in v1).
- `get_thread` walks down from a root, building a `sort_path` column so
  the result is a proper depth-first traversal with siblings ordered
  by date.

A 1000-deep depth limit guards against pathological cycles in the
underlying data (real lkml threads rarely exceed ~50 deep).
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.models import Inbox

MAX_DEPTH = 1000
ACTIVE_THREADS_CACHE_TTL_SEC = 300  # 5 minutes


@dataclass
class ThreadNode:
    id: int
    message_id: str
    thread_parent: str | None
    subject: str | None
    author: str | None
    date: datetime | None
    depth: int


@dataclass
class ActiveThread:
    """A thread that's seen activity inside the recent-window. `id`,
    `inbox_name`, `message_id`, and `date` are the root article's, so
    `|msg_url` (which expects an Article-shape) works on this directly.

    `recent_count` counts every message in the window (including the root
    if it was sent during the window). `reply_count` is the same minus
    the root — so a brand-new thread posted today with no responses yet
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


def _coerce_dt(value) -> datetime | None:
    """text() raw SQL bypasses SQLAlchemy type coercion, so DateTime columns
    come back as ISO strings. Coerce back to datetime."""
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def find_thread_root(session: Session, inbox: Inbox, message_id: str) -> str | None:
    """Return the message_id of the topmost ancestor present in this inbox.
    Walks only within the inbox (via the article_lists join) so threads
    don't span inboxes.
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


def get_thread(session: Session, inbox: Inbox, root_message_id: str) -> list[ThreadNode]:
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
    return [
        ThreadNode(
            id=r.id,
            message_id=r.message_id,
            thread_parent=r.thread_parent,
            subject=r.subject,
            author=r.author,
            date=_coerce_dt(r.date),
            depth=r.depth,
        )
        for r in rows
    ]


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
    extra_seed_filter_sql: str = "",
    extra_params: dict | None = None,
) -> list[ActiveThread]:
    """Run the active-threads recursive CTE over a `[start, end)` window.

    `order_by`: 'score' (decay-weighted) or 'last_activity' (recency).
    `limit`: None means unbounded (return every thread with at least one
    message in the window).

    `extra_seed_filter_sql`: optional SQL fragment AND-ed into the
    seed step's WHERE clause. Used by the per-subsystem dashboard
    to constrain the recursive walk to messages touching specific
    paths. Caller-supplied — must reference bind parameters only
    via the `extra_params` dict (not string-interpolated values).
    """
    order_sql = _ORDER_CLAUSES[order_by]
    limit_sql = f"LIMIT {int(limit)}" if limit is not None else ""
    sql = text(
        f"""
        WITH RECURSIVE chains AS (
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
            date=_coerce_dt(r.root_date),
            recent_count=r.recent_count,
            reply_count=r.reply_count,
            last_activity=_coerce_dt(r.last_activity),
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
        end = datetime.now(timezone.utc)
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
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
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


# Cap on the "what I missed" window — keeps the recursive CTE
# bounded for a UI surface that anyone can hit. Operators returning
# from a holiday longer than this see "showing last 90 days" rather
# than a 30-second query.
THREADS_SINCE_MAX_DAYS = 90


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
    """
    def compute() -> list[ActiveThread]:
        end = datetime.now(timezone.utc)
        floor = end - timedelta(days=THREADS_SINCE_MAX_DAYS)
        start = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
        if start < floor:
            start = floor
        if start >= end:
            return []
        return _active_threads_query(
            session, inbox, start, end,
            order_by="last_activity", limit=None,
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
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
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
