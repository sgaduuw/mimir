"""Shared SQL builder for "article IDs matching a subsystem's path
globs." Underscore-prefixed so callers see the result-builders
(`reads.py`, `reviewers.py`) as the public surface; this module is
infrastructure they share.

Split out of `reads.py` as part of the one-concern-per-file pass
(the reads.py docstring used to read "fan-outs AND the path-filter
builder", the literal "and" being the load-bearing signal).
"""
from mimir.models import Subsystem


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
