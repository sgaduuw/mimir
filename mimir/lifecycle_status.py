"""5-state lifecycle status for the listing-row pill and at-a-glance
maintainer triage. See spec
`_claude/specs/2026-05-26-multi-tree-lifecycle-design.md` for the
taxonomy rationale.

State priority (highest first): LANDED > SUPERSEDED > QUEUED >
REVIEWED > PENDING. The bulk fetcher reads `mainline_commits`,
`article_trailers`, and a self-join on `articles` (for supersedance)
in one combined SELECT; a query-plan-pin test (Task 9) ensures all
three LEFT JOIN sources stay on index seeks rather than scans
(regression guard against an ANALYZE drift).
"""

from dataclasses import dataclass
from enum import Enum

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


class LifecycleStatus(Enum):
    LANDED = "landed"
    SUPERSEDED = "superseded"
    QUEUED = "queued"
    REVIEWED = "reviewed"
    PENDING = "pending"


@dataclass
class LifecycleStatusInfo:
    """One article's status + (for QUEUED) the earliest tree it
    appeared in. Used by templates to pick a pill class + label."""

    state: LifecycleStatus
    tree: str | None = None


# Trailer roles that count as actionable review feedback. Mirrors
# `_REVIEW_TRAILER_ROLES` in `mimir/subsystems_dashboard/triage.py`.
# Signed-off-by (authorship) and Reported-by (bug-report attribution)
# are deliberately excluded.
#
# NOTE: also hardcoded inline in `_BULK_SQL` below (SQLAlchemy's
# text() can't interpolate Python tuples into raw IN clauses). When
# adding a role, update both places.
_REVIEW_ROLES = ("Reviewed-by", "Acked-by", "Tested-by")


# Use a parameterised IN(...) via SQLAlchemy text() with expanding
# bindparam. Keeps the query plan-pinned and dodges SQLite's limit
# on positional params for very large lists. Exposed at module
# level so the plan-pin test (Task 9) can EXPLAIN it.
_BULK_SQL = text("""
    WITH mc AS (
        SELECT a.id AS article_id,
               SUM(CASE WHEN c.tree_name = 'linus' THEN 1 ELSE 0 END) AS in_linus,
               (SELECT cc.tree_name FROM mainline_commits cc
                  WHERE cc.message_id = a.message_id
                    AND cc.tree_name != 'linus'
                  ORDER BY cc.committed_at ASC LIMIT 1) AS earliest_other_tree
          FROM articles a
          LEFT JOIN mainline_commits c ON c.message_id = a.message_id
         WHERE a.id IN :ids
         GROUP BY a.id
    ),
    at AS (
        SELECT t.article_id, 1 AS has_review
          FROM article_trailers t
         WHERE t.article_id IN :ids
           AND t.role IN ('Reviewed-by', 'Acked-by', 'Tested-by')
         GROUP BY t.article_id
    ),
    sup AS (
        SELECT a1.id
          FROM articles a1
          JOIN articles a2
            ON a1.patch_series_key IS NOT NULL
           AND a1.patch_series_key = a2.patch_series_key
           AND COALESCE(a1.patch_series_position, 0) =
               COALESCE(a2.patch_series_position, 0)
           AND CAST(a2.patch_series_version AS INTEGER) >
               CAST(a1.patch_series_version AS INTEGER)
         WHERE a1.id IN :ids
    )
    SELECT a.id,
           COALESCE(mc.in_linus, 0) AS in_linus,
           mc.earliest_other_tree AS earliest_other,
           (at.has_review IS NOT NULL) AS has_review,
           (sup.id IS NOT NULL) AS superseded
      FROM articles a
      LEFT JOIN mc ON mc.article_id = a.id
      LEFT JOIN at ON at.article_id = a.id
      LEFT JOIN sup ON sup.id = a.id
     WHERE a.id IN :ids
""").bindparams(bindparam("ids", expanding=True))


def _bulk_uncached(
    session: Session,
    article_ids: list[int],
) -> dict[int, LifecycleStatusInfo]:
    """Run the bulk SQL and derive the 5-state classification per
    row. Pure compute; no cache. Called by `lifecycle_status_for_
    articles` on cache misses and by the query-plan-pin test
    directly."""
    rows = session.execute(_BULK_SQL, {"ids": article_ids}).all()
    out: dict[int, LifecycleStatusInfo] = {}
    for row in rows:
        if row.in_linus and row.in_linus > 0:
            out[row.id] = LifecycleStatusInfo(LifecycleStatus.LANDED)
        elif row.superseded:
            out[row.id] = LifecycleStatusInfo(LifecycleStatus.SUPERSEDED)
        elif row.earliest_other:
            out[row.id] = LifecycleStatusInfo(
                LifecycleStatus.QUEUED,
                tree=row.earliest_other,
            )
        elif row.has_review:
            out[row.id] = LifecycleStatusInfo(LifecycleStatus.REVIEWED)
        else:
            out[row.id] = LifecycleStatusInfo(LifecycleStatus.PENDING)
    return out


def lifecycle_status_for_articles(
    session: Session,
    article_ids: list[int],
) -> dict[int, LifecycleStatusInfo]:
    """Bulk lifecycle-status fetch for a listing of articles.

    Caching layer added in Task 8; this version is uncached and
    delegates straight to `_bulk_uncached`. Empty input -> empty
    dict. Missing IDs (e.g. articles not in the corpus) are absent
    from the result.
    """
    if not article_ids:
        return {}
    return _bulk_uncached(session, article_ids)


__all__ = [
    "LifecycleStatus",
    "LifecycleStatusInfo",
    "lifecycle_status_for_articles",
]
