"""Per-subsystem read fan-outs: helpers that take `(inbox, subsystem)`
and return article-shaped results (`recent_articles_in_subsystem`,
`daily_volume_in_subsystem`, `active_threads_in_subsystem`).

The shared path-filter SQL builder lives in `_path_filter.py` so this
module covers one concern (read fan-outs) and the sibling
`reviewers.py` can pull the builder from the same neutral home.
"""
from collections import defaultdict
from datetime import date as date_cls, datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.dashboard import DAILY_VOLUME_CACHE_TTL_SEC, DailyVolume
from mimir.models import (
    ArticleList,
    Inbox,
    Subsystem,
)
from mimir.subsystems import (
    RelatedPatch,
    SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC,
)
from mimir.subsystems_dashboard._path_filter import _subsystem_path_filter_sql
from mimir.threading import ActiveThread, _active_threads_query, _coerce_dt


def recent_articles_in_subsystem(
    session: Session, inbox: Inbox, subsystem: Subsystem, limit: int = 20,
    *,
    force: bool = False,
) -> list[RelatedPatch]:
    """Recent articles linked to `inbox` whose `article_files` paths
    match any of `subsystem`'s F: globs and aren't vetoed by its
    X: globs.

    Per-inbox scoping matches the other per-inbox surfaces
    (today / yesterday / year / month). The subsystem dashboard
    URL is `/<inbox>/subsystem/<name>/`, so we want articles linked
    to *this* inbox via `article_lists`.

    Glob handling in this slice covers the two MAINTAINERS shapes
    that dominate the file: trailing-slash directory prefixes and
    exact paths. Wildcard globs (`fs/*/file.c` and friends) are
    skipped silently, they're a small minority of rules and add
    a full-table scan to every dashboard hit. A follow-up slice
    can fold them in once the simple-glob case is shipping.

    Cached for `SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC`. The path filter
    delegates to `_subsystem_path_filter_sql` so the SQL-side X:
    semantics ("at least one path is included and not excluded")
    do the work without a Python overfetch + post-filter pass; the
    earlier shape ran tens of seconds cold on busy subsystems like
    NETWORKING (#198).
    """
    def compute() -> list[RelatedPatch]:
        path_filter = _subsystem_path_filter_sql(subsystem, prefix="rasf")
        if path_filter is None:
            return []
        path_sql, path_params = path_filter
        rows = session.execute(
            text(
                f"""
                SELECT a.id AS article_id, a.message_id, a.subject,
                       a.author, a.date AS art_date,
                       a.canonical_inbox_id
                FROM articles a
                JOIN article_lists al ON al.article_id = a.id
                WHERE al.inbox_id = :inbox_id
                  AND a.id IN ({path_sql})
                ORDER BY a.date DESC
                LIMIT :limit
                """
            ),
            {
                "inbox_id": inbox.id,
                "limit": limit,
                **path_params,
            },
        ).all()
        if not rows:
            return []

        # Resolve inbox names for the canonical-or-fallback URL building,
        # one bulk query.
        valid_ids = [r.article_id for r in rows]
        links = session.execute(
            select(ArticleList.article_id, Inbox.id, Inbox.name)
            .join(Inbox, Inbox.id == ArticleList.inbox_id)
            .where(ArticleList.article_id.in_(valid_ids))
        ).all()
        links_by_article: dict[int, list[tuple[int, str]]] = defaultdict(list)
        for art_id, ix_id, ix_name in links:
            links_by_article[art_id].append((ix_id, ix_name))

        out: list[RelatedPatch] = []
        for r in rows:
            link_set = links_by_article.get(r.article_id, [])
            canon_name: str | None = None
            if r.canonical_inbox_id is not None:
                for ix_id, name in link_set:
                    if ix_id == r.canonical_inbox_id:
                        canon_name = name
                        break
            if canon_name is None and link_set:
                canon_name = min(name for _, name in link_set)
            if canon_name is None:
                continue
            out.append(RelatedPatch(
                article_id=r.article_id,
                message_id=r.message_id,
                subject=r.subject,
                author=r.author,
                date=_coerce_dt(r.art_date),
                inbox_name=canon_name,
            ))
        return out

    return cache.get_or_compute(
        session,
        f"recent_articles_in_subsystem:{inbox.name}:{subsystem.id}:{limit}",
        SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def daily_volume_in_subsystem(
    session: Session, inbox: Inbox, subsystem: Subsystem,
    days: int = 30, force: bool = False,
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
        today = date_cls.today()
        start = today - timedelta(days=days - 1)
        path_filter = _subsystem_path_filter_sql(subsystem, prefix="dvss")
        if path_filter is None:
            return DailyVolume(
                days=[
                    (start + timedelta(days=i), 0)
                    for i in range(days)
                ],
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
    session: Session, inbox: Inbox, subsystem: Subsystem,
    days: int = 7, limit: int = 10, force: bool = False,
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
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        # Materialise the path-matched article ids once before the
        # recursive CTE runs. Without `MATERIALIZED`, SQLite inlines
        # the CTE and re-evaluates the `article_files` lookup per
        # seed row, order-of-seconds on wide subsystems like
        # "open firmware and flattened device tree bindings" with
        # many F: globs. The temp-table form runs the lookup once.
        return _active_threads_query(
            session, inbox, start, end,
            order_by="score", limit=limit,
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
