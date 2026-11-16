"""Shared SQL builder for "article IDs matching a subsystem's path
globs." Underscore-prefixed so callers see the result-builders
(`reads.py`, `reviewers.py`) as the public surface; this module is
infrastructure they share.

Split out of `reads.py` as part of the one-concern-per-file pass
(the reads.py docstring used to read "fan-outs AND the path-filter
builder", the literal "and" being the load-bearing signal).
"""

import sqlite3

from sqlalchemy import text
from sqlalchemy.orm import Session

from mimir.models import Subsystem


def _prefix_bounds(g: str) -> tuple[str, str]:
    """Half-open `[lo, hi)` byte range covering every path that
    starts with the directory prefix `g` (e.g. `arch/x86_64/`).
    Bumping the trailing byte by one is sufficient because SQLite
    compares TEXT as BLOB-style byte sequences."""
    return g, g[:-1] + chr(ord(g[-1]) + 1)


def _subsystem_path_filter_sql(
    subsystem: Subsystem,
    prefix: str = "ssp",
    *,
    apply_excludes: bool = True,
) -> tuple[str, dict] | None:
    """Return `(sql, params)` where `sql` is a SELECT that enumerates
    article IDs matching the subsystem's F: globs and not vetoed by
    its X: globs. IDs may repeat: membership callers use IN, so a
    global DISTINCT sort would be wasted work. `params` is the bind-parameter
    dict to pass to `text()`. `apply_excludes=False` is only for the
    conservative raw-work probe, never for selecting matching articles.

    Per-path semantics: an article belongs to the subsystem if at
    least one of its `article_files.path` rows is matched by an
    include and not by any exclude. The SQL realises that as a
    UNION ALL of independent index seeks, one per F: rule, each AND-ed
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
    `recent_postings_in_subsystem`.
    """
    includes = [r.glob for r in subsystem.paths if not r.is_exclude]
    excludes = (
        [r.glob for r in subsystem.paths if r.is_exclude] if apply_excludes else []
    )
    params: dict[str, str] = {}

    def _add(name: str, value: str) -> str:
        params[name] = value
        return name

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
    exc_clause = " AND NOT (" + " OR ".join(exc_parts) + ")" if exc_parts else ""

    branches: list[str] = []
    # Pin each branch to its path bounds, rather than letting the planner
    # choose the article/path covering index for these path-driven reads.
    for i, g in enumerate(includes):
        if g.endswith("/"):
            lo, hi = _prefix_bounds(g)
            pname_lo = _add(f"{prefix}_inc_lo_{i}", lo)
            pname_hi = _add(f"{prefix}_inc_hi_{i}", hi)
            branches.append(
                "SELECT article_id FROM article_files INDEXED BY ix_article_files_path "
                f"WHERE path >= :{pname_lo} AND path < :{pname_hi}{exc_clause}"
            )
            # Also match the bare directory path (no trailing slash).
            pname_eq = _add(f"{prefix}_inc_eq_{i}", g[:-1])
            branches.append(
                "SELECT article_id FROM article_files INDEXED BY ix_article_files_path "
                f"WHERE path = :{pname_eq}{exc_clause}"
            )
        elif not any(c in g for c in "*?["):
            pname = _add(f"{prefix}_inc_eq_{i}", g)
            branches.append(
                "SELECT article_id FROM article_files INDEXED BY ix_article_files_path "
                f"WHERE path = :{pname}{exc_clause}"
            )
        # else: wildcard skipped (slice 1/2)

    if not branches:
        return None
    return " UNION ALL ".join(branches), params


def _path_seek_is_bounded(
    session: Session, subsystem: Subsystem, max_matches: int
) -> bool:
    """Conservatively bound path-index work before choosing a path-first query.

    Count raw include rows, including duplicates and excluded paths: filtering
    X: rules before LIMIT could scan a huge include range that produces no
    survivors. Projection to 1 keeps the probe on the covering path index.
    This chooses a query plan, never truncates the result or caches candidates.
    """
    built = _subsystem_path_filter_sql(
        subsystem, prefix="path_budget", apply_excludes=False
    )
    if built is None:
        return True
    sql, params = built
    connection = session.connection().connection.driver_connection
    compound_limit = connection.getlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT)
    # Zero means unlimited. A probe must not reject a working EXISTS query.
    if compound_limit > 0 and sql.count(" UNION ALL ") + 1 > compound_limit:
        return False
    count = session.scalar(
        text(f"SELECT count(*) FROM (SELECT 1 FROM ({sql}) LIMIT :path_budget_limit)"),
        {**params, "path_budget_limit": max_matches + 1},
    )
    return count <= max_matches


def _subsystem_path_filter_exists_sql(
    subsystem: Subsystem,
    prefix: str = "spe",
) -> tuple[str, dict] | None:
    """Sibling of `_subsystem_path_filter_sql` that returns a
    correlated `EXISTS (...)` predicate fragment rather than an
    article_id enumerator. Use when the outer query wants to walk
    a different index (typically `ix_articles_date`) and test the
    path filter per article, instead of materialising the full set
    of matching article_ids and joining.

    Returns `(predicate_sql, params)` where `predicate_sql` is a
    boolean expression to AND into an outer WHERE. The expression
    references `a.id` as the correlation column.
    Returns `None` when the subsystem has no supported (non-
    wildcard) include rules; caller treats that as "no match" and
    skips the query entirely.

    Why a separate helper: the UNION-of-seeks shape in
    `_subsystem_path_filter_sql` is right when the path filter is
    the most selective predicate AND the outer query has no other
    order/limit. The triage queues invert that: they walk
    `ix_articles_date` ASC, ORDER BY ... LIMIT 10, and the
    path filter is the *cheapest per-row* predicate (an article
    has ~3 files on average, indexed by `(article_id, path)` PK).
    Walking the date index ASC + per-row EXISTS gets the planner
    to a "test 10-N articles to find 10 hits" shape and avoids
    materialising the full subsystem's article_id set, which on
    NETWORKING [GENERAL] runs into the hundreds of thousands and
    drives the IN-list variant to ~8 s cold.
    """
    includes = [r.glob for r in subsystem.paths if not r.is_exclude]
    excludes = [r.glob for r in subsystem.paths if r.is_exclude]
    params: dict[str, str] = {}

    def _add(name: str, value: str) -> str:
        params[name] = value
        return name

    # Build the include OR-list. Skip wildcards (same slice-1/2
    # rationale as the sibling helper).
    inc_parts: list[str] = []
    for i, g in enumerate(includes):
        if g.endswith("/"):
            lo, hi = _prefix_bounds(g)
            pname_lo = _add(f"{prefix}_inc_lo_{i}", lo)
            pname_hi = _add(f"{prefix}_inc_hi_{i}", hi)
            inc_parts.append(f"(af.path >= :{pname_lo} AND af.path < :{pname_hi})")
            pname_eq = _add(f"{prefix}_inc_eq_{i}", g[:-1])
            inc_parts.append(f"af.path = :{pname_eq}")
        elif not any(c in g for c in "*?["):
            pname = _add(f"{prefix}_inc_eq_{i}", g)
            inc_parts.append(f"af.path = :{pname}")
        # else: wildcard skipped
    if not inc_parts:
        return None
    include_or = " OR ".join(inc_parts)

    # Exclude OR-list AND-NOT-ed against the include match. Same
    # per-row shape as the sibling helper (the exclude check rides
    # on already-narrow include matches).
    exc_parts: list[str] = []
    for i, g in enumerate(excludes):
        if g.endswith("/"):
            lo, hi = _prefix_bounds(g)
            pname_lo = _add(f"{prefix}_exc_lo_{i}", lo)
            pname_hi = _add(f"{prefix}_exc_hi_{i}", hi)
            exc_parts.append(f"(af.path >= :{pname_lo} AND af.path < :{pname_hi})")
            pname_eq = _add(f"{prefix}_exc_eq_{i}", g[:-1])
            exc_parts.append(f"af.path = :{pname_eq}")
        elif not any(c in g for c in "*?["):
            pname = _add(f"{prefix}_exc_eq_{i}", g)
            exc_parts.append(f"af.path = :{pname}")
    exc_clause = f" AND NOT ({' OR '.join(exc_parts)})" if exc_parts else ""

    predicate = (
        f"EXISTS (SELECT 1 FROM article_files af "
        "WHERE af.article_id = a.id "
        f"AND ({include_or}){exc_clause})"
    )
    return predicate, params
