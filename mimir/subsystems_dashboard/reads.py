"""Per-subsystem read fan-outs: helpers that take `(inbox, subsystem)`
and return daily volume and active threads. Grouped recent postings
live in the sibling `recent` module.

The shared path-filter SQL builder lives in `_path_filter.py` so this
module covers one concern (read fan-outs) and the sibling
`reviewers.py` can pull the builder from the same neutral home.
"""

from datetime import UTC, datetime, timedelta
from datetime import date as date_cls

from sqlalchemy import text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.dashboard import DAILY_VOLUME_CACHE_TTL_SEC, DailyVolume
from mimir.models import (
    Inbox,
    Subsystem,
)
from mimir.subsystems import (
    SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC,
)
from mimir.subsystems_dashboard._path_filter import _subsystem_path_filter_sql
from mimir.threading import ActiveThread, _active_threads_query


def daily_volume_in_subsystem(
    session: Session,
    inbox: Inbox,
    subsystem: Subsystem,
    days: int = 30,
    force: bool = False,
) -> DailyVolume:
    """Daily message counts in `inbox` for articles whose paths
    match `subsystem`'s F: globs (minus X: vetoes) over the last
    `days` days, zero-filled. Cached per `(inbox, subsystem, days)`
    key for the same TTL as the global `daily_volume`.

    Returns an empty-but-zero-filled series (all zeros) when the
    subsystem has no supported globs; the sparkline still renders,
    just flat.
    """

    def compute() -> DailyVolume:
        # `Article.date` is the public-inbox commit time in UTC (see
        # CONTEXT.md "articles.date = public-inbox commit timestamp"),
        # so the bucket window must use the UTC date too. `date.today()`
        # would advance at *local* midnight, dropping boundary-day
        # articles outside the range on any non-UTC TZ.
        today = datetime.now(UTC).date()
        start = today - timedelta(days=days - 1)
        path_filter = _subsystem_path_filter_sql(subsystem, prefix="dvss")
        if path_filter is None:
            return DailyVolume(
                days=[(start + timedelta(days=i), 0) for i in range(days)],
                max_count=1,
            )
        path_sql, path_params = path_filter
        rows = session.execute(
            text(
                f"""
                SELECT date(a.date) AS day, COUNT(*) AS n
                FROM articles a
                JOIN article_lists al ON al.article_id = a.id
                WHERE al.inbox_id = :inbox_id AND a.date >= :start
                  AND a.id IN ({path_sql})
                GROUP BY day
                """
            ),
            {"inbox_id": inbox.id, "start": start.isoformat(), **path_params},
        ).all()
        counts = {date_cls.fromisoformat(r.day): r.n for r in rows if r.day}
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
        f"daily_volume_in_subsystem:{inbox.name}:{subsystem.id}:{days}",
        DAILY_VOLUME_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def active_threads_in_subsystem(
    session: Session,
    inbox: Inbox,
    subsystem: Subsystem,
    days: int = 7,
    limit: int = 10,
    force: bool = False,
) -> list[ActiveThread]:
    """Most-active threads in `inbox` over the last `days` days
    among messages whose paths match `subsystem`'s F: globs (minus
    X: vetoes). Same decay-weighted score as the landing-page
    `active_threads`; the only difference is the seed-set filter.

    Returns an empty list when the subsystem has no supported
    globs (slice 1/2 ignores wildcard rules). Cached per
    `(inbox, subsystem, days, limit)` at the 1h per-subsystem
    dashboard TTL, the recursive CTE is too heavy for the 5min
    front-page real-time feel, and visitors landing on
    `/<inbox>/subsystem/<name>/` are reading the page, not watching
    for live updates.
    """

    def compute() -> list[ActiveThread]:
        path_filter = _subsystem_path_filter_sql(subsystem, prefix="atss")
        if path_filter is None:
            return []
        path_sql, path_params = path_filter
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        # Materialise the path-matched article ids once before the
        # recursive CTE runs. Without `MATERIALIZED`, SQLite inlines
        # the CTE and re-evaluates the `article_files` lookup per
        # seed row, order-of-seconds on wide subsystems like
        # "open firmware and flattened device tree bindings" with
        # many F: globs. The temp-table form runs the lookup once.
        return _active_threads_query(
            session,
            inbox,
            start,
            end,
            order_by="score",
            limit=limit,
            extra_ctes_sql=f"path_articles AS MATERIALIZED ({path_sql}),",
            extra_seed_filter_sql=" AND a.id IN (SELECT article_id FROM path_articles)",
            extra_params=path_params,
        )

    return cache.get_or_compute(
        session,
        f"active_threads_in_subsystem:{inbox.name}:{subsystem.id}:{days}:{limit}",
        SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC,
        compute,
        force=force,
    )
