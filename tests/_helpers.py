"""Helpers shared by test modules."""

from contextlib import contextmanager

from sqlalchemy import text

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
