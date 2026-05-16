"""Per-subsystem read fan-outs and the shared path-filter SQL builder.

Owns the helpers that take `(inbox, subsystem)` and return article-shaped
results: `recent_articles_in_subsystem`, `daily_volume_in_subsystem`,
`active_threads_in_subsystem`. The shared `_subsystem_path_filter_sql`
builder also lives here because both the daily-volume and active-threads
helpers need it, and `active_reviewers_in_subsystem` (in `.reviewers`)
imports it from here.
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


def _subsystem_path_filter_sql(
    subsystem: Subsystem, prefix: str = "ssp",
) -> tuple[str, dict] | None:
    """Return `(sql, params)` where `sql` is a SELECT that enumerates
    article IDs matching the subsystem's F: globs and not vetoed by
    its X: globs. `params` is the bind-parameter dict to pass to
    `text()`.

    Per-path semantics: an article belongs to the subsystem if at
    least one of its `article_files.path` rows is matched by an
    include and not by any exclude. The SQL realises that as a
    UNION of independent index seeks, one per F: rule, each AND-ed
    with a per-row NOT-any-exclude predicate.

    Why UNION-of-seeks and not OR-of-LIKEs: SQLite's LIKE→range-scan
    optimisation is disabled whenever ESCAPE is present, and the
    earlier shape needed ESCAPE so a glob containing `_` (e.g.
    `arch/x86_64/`) wouldn't widen the match. Every prefix-LIKE
    branch therefore fell back to a full scan of `article_files`
    (millions of rows) per dashboard render, NETWORKING [GENERAL]
    on lkml ran ~10 s cold (#198). Range comparisons (`path >= lo
    AND path < hi`) treat `_` as the literal byte it is and let each
    branch use `ix_article_files_path` as a sargable seek; the
    NETWORKING cold miss drops to ~400 ms. Plan pinned in
    test_subsystem_path_filter_uses_index_seeks.

    Returns `None` when the subsystem has no supported (non-
    wildcard) include rules, caller should treat that as "no
    articles" rather than running an unfiltered query. Wildcard
    F: rules (`fs/*/file.c`-style) are skipped silently in this
    slice for the same reason they're skipped in
    `recent_articles_in_subsystem`.
    """
    includes = [r.glob for r in subsystem.paths if not r.is_exclude]
    excludes = [r.glob for r in subsystem.paths if r.is_exclude]
    params: dict[str, str] = {}

    def _add(name: str, value: str) -> str:
        params[name] = value
        return name

    def _prefix_bounds(g: str) -> tuple[str, str]:
        """Half-open `[lo, hi)` byte range covering every path that
        starts with the directory prefix `g` (e.g. `arch/x86_64/`).
        Bumping the trailing byte by one is sufficient because SQLite
        compares TEXT as BLOB-style byte sequences."""
        return g, g[:-1] + chr(ord(g[-1]) + 1)

    # Per-row NOT(exclude) predicate AND-ed into every UNION branch.
    # Small disjunction, only evaluated on rows already matched by an
    # include seek, so cost is bounded by the include result size
    # rather than the size of article_files.
    exc_parts: list[str] = []
    for i, g in enumerate(excludes):
        if g.endswith("/"):
            lo, hi = _prefix_bounds(g)
            pname_lo = _add(f"{prefix}_exc_lo_{i}", lo)
            pname_hi = _add(f"{prefix}_exc_hi_{i}", hi)
            exc_parts.append(f"(path >= :{pname_lo} AND path < :{pname_hi})")
            pname_eq = _add(f"{prefix}_exc_eq_{i}", g[:-1])
            exc_parts.append(f"path = :{pname_eq}")
        elif not any(c in g for c in "*?["):
            pname = _add(f"{prefix}_exc_eq_{i}", g)
            exc_parts.append(f"path = :{pname}")
        # else: wildcard skipped (slice 1/2)
    exc_clause = (
        " AND NOT (" + " OR ".join(exc_parts) + ")" if exc_parts else ""
    )

    branches: list[str] = []
    for i, g in enumerate(includes):
        if g.endswith("/"):
            lo, hi = _prefix_bounds(g)
            pname_lo = _add(f"{prefix}_inc_lo_{i}", lo)
            pname_hi = _add(f"{prefix}_inc_hi_{i}", hi)
            branches.append(
                "SELECT article_id FROM article_files "
                f"WHERE path >= :{pname_lo} AND path < :{pname_hi}{exc_clause}"
            )
            # Also match the bare directory path (no trailing slash).
            pname_eq = _add(f"{prefix}_inc_eq_{i}", g[:-1])
            branches.append(
                "SELECT article_id FROM article_files "
                f"WHERE path = :{pname_eq}{exc_clause}"
            )
        elif not any(c in g for c in "*?["):
            pname = _add(f"{prefix}_inc_eq_{i}", g)
            branches.append(
                "SELECT article_id FROM article_files "
                f"WHERE path = :{pname}{exc_clause}"
            )
        # else: wildcard skipped (slice 1/2)

    if not branches:
        return None
    return " UNION ".join(branches), params


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
