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

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.dashboard import DAILY_VOLUME_CACHE_TTL_SEC, DailyVolume, like_escape
from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    Inbox,
    Subsystem,
)
from mimir.subsystems import (
    RelatedPatch,
    SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC,
    path_matches_glob,
)
from mimir.threading import ActiveThread, _active_threads_query


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

    Cached for `SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC`. The query joins
    article_files × article_lists × articles with one OR clause per
    F: glob (large subsystems like DT-bindings have many), then a
    Python-side X: filter walk; on a multi-million-row archive that
    runs into double-digit seconds for the busy subsystems. Warm-
    cache pre-builds the top-N most active subsystems per inbox so
    most readers landing on a hot dashboard get a cache hit.
    """
    def compute() -> list[RelatedPatch]:
        includes = [r for r in subsystem.paths if not r.is_exclude]
        excludes = [r.glob for r in subsystem.paths if r.is_exclude]

        # Build OR conditions for each supported include glob. Exact
        # paths (the modal MAINTAINERS shape: explicit `F: drivers/
        # foo/bar.c` lines) are collapsed into a single `path IN
        # (...)` clause instead of one OR-equality per rule, wide
        # subsystems can list dozens of files, and IN lets SQLite
        # build one in-memory probe instead of walking a long OR
        # disjunction. Directory-prefix `dir/` entries still need
        # one LIKE per rule (each escapes its own metacharacters).
        exact_paths: list[str] = []
        or_conds = []
        for rule in includes:
            g = rule.glob
            if g.endswith("/"):
                # Directory prefix. SQLite LIKE doesn't treat `_` and `%`
                # as literal, escape them so a path glob containing `_`
                # (rare but possible: `arch/x86_64/`) doesn't widen the
                # match.
                prefix = like_escape(g)
                or_conds.append(ArticleFile.path.like(prefix + "%", escape="\\"))
                # Also match the bare directory path (no trailing slash).
                exact_paths.append(g[:-1])
            elif not any(c in g for c in "*?["):
                exact_paths.append(g)
            # else: wildcard, skipped in slice 1
        if exact_paths:
            or_conds.append(ArticleFile.path.in_(exact_paths))
        if not or_conds:
            return []

        # Over-fetch by a factor so the X: exclude pass below has room
        # to filter without starving the result list.
        overfetch = max(limit * 3, 60)
        rows = session.execute(
            select(Article.id, Article.message_id, Article.subject,
                   Article.author, Article.date,
                   Article.canonical_inbox_id,
                   ArticleFile.path)
            .join(ArticleFile, ArticleFile.article_id == Article.id)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(or_(*or_conds), ArticleList.inbox_id == inbox.id)
            .order_by(Article.date.desc())
            .limit(overfetch)
        ).all()

        # Group touched paths per article so the X: filter can see every
        # path a given article touched (not just the one row that matched
        # an include).
        art_paths: dict[int, set[str]] = defaultdict(set)
        art_data: dict[int, tuple] = {}
        art_order: list[int] = []  # preserve newest-first order from SQL
        for art_id, mid, subj, author, date, canon_id, path in rows:
            if art_id not in art_data:
                art_data[art_id] = (mid, subj, author, date, canon_id)
                art_order.append(art_id)
            art_paths[art_id].add(path)

        # The X: pass operates over the per-article path set: a subsystem
        # vetoes an article only if *every* matched path is excluded. If
        # at least one path remains in-scope after applying X:, the
        # article still belongs to the subsystem.
        valid_ids: list[int] = []
        for art_id in art_order:
            in_scope = [
                p for p in art_paths[art_id]
                if any(
                    path_matches_glob(p, inc.glob) for inc in includes
                ) and not any(
                    path_matches_glob(p, x) for x in excludes
                )
            ]
            if in_scope:
                valid_ids.append(art_id)
                if len(valid_ids) >= limit:
                    break
        if not valid_ids:
            return []

        # Resolve inbox names for the canonical-or-fallback URL building,
        # one bulk query.
        links = session.execute(
            select(ArticleList.article_id, Inbox.id, Inbox.name)
            .join(Inbox, Inbox.id == ArticleList.inbox_id)
            .where(ArticleList.article_id.in_(valid_ids))
        ).all()
        links_by_article: dict[int, list[tuple[int, str]]] = defaultdict(list)
        for art_id, ix_id, ix_name in links:
            links_by_article[art_id].append((ix_id, ix_name))

        out: list[RelatedPatch] = []
        for art_id in valid_ids:
            mid, subj, author, date, canon_id = art_data[art_id]
            link_set = links_by_article.get(art_id, [])
            canon_name: str | None = None
            if canon_id is not None:
                for ix_id, name in link_set:
                    if ix_id == canon_id:
                        canon_name = name
                        break
            if canon_name is None and link_set:
                canon_name = min(name for _, name in link_set)
            if canon_name is None:
                continue
            out.append(RelatedPatch(
                article_id=art_id, message_id=mid, subject=subj,
                author=author, date=date, inbox_name=canon_name,
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
    """Return `(sql, params)` where `sql` is a
    `SELECT article_id FROM article_files WHERE ...` clause that
    enumerates article IDs matching the subsystem's F: globs and
    not vetoed by its X: globs. `params` is the bind-parameter
    dict to pass to `text()`.

    Per-path semantics: an article belongs to the subsystem if at
    least one of its `article_files.path` rows is matched by an
    include and not by any exclude. Same rule as the in-memory
    pass in `recent_articles_in_subsystem`.

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

    def build(globs: list[str], label: str) -> str:
        parts: list[str] = []
        for i, g in enumerate(globs):
            if g.endswith("/"):
                pname_pre = f"{prefix}_{label}_pre_{i}"
                # Escape LIKE wildcards so a glob containing `_`
                # (e.g. `arch/x86_64/`) doesn't widen the match.
                params[pname_pre] = like_escape(g) + "%"
                parts.append(f"path LIKE :{pname_pre} ESCAPE '\\'")
                # Also include the bare directory path.
                pname_eq = f"{prefix}_{label}_eq_{i}"
                params[pname_eq] = g[:-1]
                parts.append(f"path = :{pname_eq}")
            elif not any(c in g for c in "*?["):
                pname = f"{prefix}_{label}_eq_{i}"
                params[pname] = g
                parts.append(f"path = :{pname}")
            # else: wildcard skipped (slice 1/2)
        return " OR ".join(parts)

    inc_sql = build(includes, "inc")
    if not inc_sql:
        return None
    sql = f"SELECT article_id FROM article_files WHERE ({inc_sql})"
    exc_sql = build(excludes, "exc")
    if exc_sql:
        sql += f" AND NOT ({exc_sql})"
    return sql, params


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
