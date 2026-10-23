"""Helpers shared by test modules."""

from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy import event, text

from mimir import cache
from mimir.dashboard import ArchiveStats, _archive_stats_key


def _cache_total(inbox, total):
    """Give `inbox` a live `archive_stats` row of `total` articles."""
    cache.set(
        _archive_stats_key(inbox),
        ArchiveStats(total=total, epochs=1, first_date=None, last_date=None),
        3600,
    )


@contextmanager
def planner_stats(session_factory, rows=()):
    """Run with `sqlite_stat1` holding exactly `rows` ((table, index,
    stat) tuples), then put back what was there. The table lives in the
    shared test database and other tests' ANALYZE leaves rows behind,
    so a plan or a step budget that depends on statistics must pin them
    and not inherit them."""
    with session_factory() as s:
        has_stats = s.execute(
            text("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'")
        ).first()
        before = (
            [tuple(r) for r in s.execute(text("SELECT * FROM sqlite_stat1"))]
            if has_stats
            else []
        )

        def load(stats):
            s.execute(text("ANALYZE"))  # creates the table when absent
            s.execute(text("DELETE FROM sqlite_stat1"))
            for tbl, idx, stat in stats:
                s.execute(
                    text("INSERT INTO sqlite_stat1 VALUES (:t, :i, :s)"),
                    {"t": tbl, "i": idx, "s": stat},
                )
            s.commit()
            # SQLite reads statistics per connection: drop the pooled
            # ones so the next connection reads the table afresh.
            s.get_bind().dispose()

        load(rows)
        try:
            yield
        finally:
            load(before)


@contextmanager
def count_steps(session):
    """Count SQLite VM steps run on `session`'s connection, to the
    nearest thousand, as `.count`. Steps do not depend on the machine's
    speed, so a budget on them is a stable work bound."""
    raw = session.connection().connection.driver_connection
    steps = SimpleNamespace(count=0)

    def tick():
        steps.count += 1000

    raw.set_progress_handler(tick, 1000)
    try:
        yield steps
    finally:
        raw.set_progress_handler(None, 0)


def plan_of(session, call, match):
    """EXPLAIN QUERY PLAN of the one statement containing `match` that
    `call` really runs, so a rewrite of a query cannot slip past a copy
    of it."""
    seen = []
    engine = session.get_bind()

    def grab(conn, cursor, statement, parameters, context, executemany):
        if match in statement:
            seen.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", grab)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    assert len(seen) == 1, seen
    statement, params = seen[0]
    raw = session.connection().connection.driver_connection
    return "\n".join(
        row[3] for row in raw.execute("EXPLAIN QUERY PLAN " + statement, params)
    )


# Production's sqlite_stat1 rows for article_lists, article_trailers and
# the date index (read-only, 2026-10-23), for plan tests that must see
# the plan production gets.
PRODUCTION_STATS = (
    ("article_lists", "ix_article_lists_inbox_id", "29764294 4001"),
    ("article_lists", "ix_article_lists_thread_root", "29764294 4001 4"),
    ("article_lists", "sqlite_autoindex_article_lists_1", "29764294 2 1"),
    ("article_trailers", "ix_article_trailers_article_id", "3561252 2"),
    ("article_trailers", "ix_article_trailers_role_addr", "3561252 4001 23"),
    ("articles", "ix_articles_date", "17790565 1"),
)
