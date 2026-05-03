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
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from mimir import cache

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
    """A thread that's seen activity inside the recent-window. `message_id`
    and `date` are the root's, so `|msg_url` (which expects an Article-shape)
    works on this directly."""
    message_id: str
    subject: str | None
    author: str | None
    date: datetime | None
    recent_count: int
    last_activity: datetime | None


def _coerce_dt(value) -> datetime | None:
    """text() raw SQL bypasses SQLAlchemy type coercion, so DateTime columns
    come back as ISO strings. Coerce back to datetime."""
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def find_thread_root(session: Session, message_id: str) -> str | None:
    """Return the message_id of the topmost ancestor present in our DB.

    If `message_id` isn't in the DB at all, returns None. A message with
    no in_reply_to (or whose in_reply_to points to a missing message) is
    its own root.
    """
    sql = text(
        """
        WITH RECURSIVE ancestors AS (
            SELECT message_id, thread_parent, 0 AS depth
            FROM articles WHERE message_id = :mid
            UNION ALL
            SELECT a.message_id, a.thread_parent, anc.depth + 1
            FROM articles a JOIN ancestors anc ON a.message_id = anc.thread_parent
            WHERE anc.depth < :max_depth
        )
        SELECT message_id FROM ancestors ORDER BY depth DESC LIMIT 1
        """
    )
    return session.execute(sql, {"mid": message_id, "max_depth": MAX_DEPTH}).scalar()


def get_thread(session: Session, root_message_id: str) -> list[ThreadNode]:
    """Return the full thread rooted at `root_message_id`, depth-first by date.

    The CTE builds a `sort_path` of "<date>|<id>" segments joined by '/';
    sorting by it gives a natural depth-first traversal with siblings
    ordered by date.
    """
    sql = text(
        """
        WITH RECURSIVE thread AS (
            SELECT id, message_id, thread_parent, subject, author, date,
                   0 AS depth,
                   CAST(date AS TEXT) || '|' || printf('%020d', id) AS sort_path
            FROM articles WHERE message_id = :root
            UNION ALL
            SELECT a.id, a.message_id, a.thread_parent, a.subject, a.author, a.date,
                   t.depth + 1,
                   t.sort_path || '/' || CAST(a.date AS TEXT) || '|' || printf('%020d', a.id)
            FROM articles a JOIN thread t ON a.thread_parent = t.message_id
            WHERE t.depth < :max_depth
        )
        SELECT id, message_id, thread_parent, subject, author, date, depth
        FROM thread
        ORDER BY sort_path
        """
    )
    rows = session.execute(sql, {"root": root_message_id, "max_depth": MAX_DEPTH}).all()
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


def active_threads(
    session: Session,
    days: int = 7,
    limit: int = 10,
    force: bool = False,
) -> list[ActiveThread]:
    """Return the most active threads in the last `days` days, by reply count.

    The recursive CTE walks up `thread_parent` from each recent message to
    its topmost in-DB ancestor, then aggregates. ~700 ms over 6.2M rows on
    a 12k-message window — too slow for hot paths, so results are cached
    on disk for ACTIVE_THREADS_CACHE_TTL_SEC (5 min) per (days, limit) key.
    Pass force=True to bypass the cache and recompute (used by the
    `warm-cache` CLI).
    """
    cache_key = f"active_threads:{days}:{limit}"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    # Score = sum of pow(0.5, age_in_days) per recent message in the thread.
    # Half-life is 1 day: a message from this hour counts as ~1.0; one from
    # 24h ago as 0.5; one from 7d ago as ~0.008. The result is that fresh
    # bursts of activity beat slow steady chatter when ranking. We still
    # show the raw COUNT(*) ("N messages in 7d") in the UI for context.
    sql = text(
        f"""
        WITH RECURSIVE chains AS (
            SELECT id AS recent_id, message_id AS curr, thread_parent,
                   date AS recent_date, 0 AS depth
            FROM articles
            WHERE date >= datetime('now', '-{int(days)} days')
            UNION ALL
            SELECT c.recent_id, a.message_id, a.thread_parent,
                   c.recent_date, c.depth + 1
            FROM articles a JOIN chains c ON a.message_id = c.thread_parent
            WHERE c.depth < :max_depth
        ),
        max_depth AS (
            SELECT recent_id, MAX(depth) AS d FROM chains GROUP BY recent_id
        ),
        roots AS (
            SELECT c.recent_id, c.curr AS root_id, c.recent_date
            FROM chains c
            JOIN max_depth m ON m.recent_id = c.recent_id AND m.d = c.depth
        )
        SELECT a.message_id, a.subject, a.author, a.date AS root_date,
               COUNT(*) AS recent_count,
               MAX(r.recent_date) AS last_activity,
               SUM(pow(0.5, julianday('now') - julianday(r.recent_date))) AS score
        FROM roots r JOIN articles a ON a.message_id = r.root_id
        GROUP BY r.root_id
        ORDER BY score DESC, last_activity DESC
        LIMIT :limit
        """
    )
    rows = session.execute(sql, {"max_depth": MAX_DEPTH, "limit": limit}).all()
    result = [
        ActiveThread(
            message_id=r.message_id,
            subject=r.subject,
            author=r.author,
            date=_coerce_dt(r.root_date),
            recent_count=r.recent_count,
            last_activity=_coerce_dt(r.last_activity),
        )
        for r in rows
    ]
    cache.set(cache_key, result, ttl=ACTIVE_THREADS_CACHE_TTL_SEC)
    return result
