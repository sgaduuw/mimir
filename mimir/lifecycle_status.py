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

from mimir import cache


class LifecycleStatus(Enum):
    LANDED = "landed"
    SUPERSEDED = "superseded"
    QUEUED = "queued"
    REVIEWED = "reviewed"
    PENDING = "pending"


@dataclass
class LifecycleStatusInfo:
    """One article's lifecycle status + display data.

    `state_value` is the canonical state name (one of
    `LifecycleStatus` values, stored as string for cache
    round-trip). `tree` is the earliest non-Linus tree for the
    QUEUED state, None otherwise.

    The remaining display-derivation fields are pre-computed by
    `_bulk_uncached` so templates render without further
    Python-side logic. They default to dormant / empty so cache
    rows from prior versions deserialize cleanly during rollout."""

    state_value: str
    tree: str | None = None
    activity_heat: str = "dormant"
    activity_detail: str = "no replies"
    pill_label: str = ""
    count_suffix: str | None = None
    tooltip: str | None = None

    @property
    def state(self) -> LifecycleStatus:
        return LifecycleStatus(self.state_value)


cache.register("LifecycleStatusInfo", LifecycleStatusInfo)

LIFECYCLE_STATUS_TTL_SEC = 300


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
            out[row.id] = LifecycleStatusInfo(state_value=LifecycleStatus.LANDED.value)
        elif row.superseded:
            out[row.id] = LifecycleStatusInfo(
                state_value=LifecycleStatus.SUPERSEDED.value
            )
        elif row.earliest_other:
            out[row.id] = LifecycleStatusInfo(
                state_value=LifecycleStatus.QUEUED.value,
                tree=row.earliest_other,
            )
        elif row.has_review:
            out[row.id] = LifecycleStatusInfo(
                state_value=LifecycleStatus.REVIEWED.value
            )
        else:
            out[row.id] = LifecycleStatusInfo(state_value=LifecycleStatus.PENDING.value)
    return out


def lifecycle_status_for_articles(
    session: Session,
    article_ids: list[int],
) -> dict[int, LifecycleStatusInfo]:
    """Bulk lifecycle-status fetch for a listing of articles.

    Per-article cache rows with a 5-minute TTL. Cache key
    `lifecycle_status:<id>`, backed by `cache.get_many` so a
    single SELECT covers all hits. Misses are computed together
    via one combined SQL call in `_bulk_uncached`. Empty input
    -> empty dict. Missing IDs (e.g. articles not in the corpus)
    are absent from the result.
    """
    if not article_ids:
        return {}
    keys = [f"lifecycle_status:{a}" for a in article_ids]
    cached = cache.get_many(keys)
    out: dict[int, LifecycleStatusInfo] = {
        int(k.split(":")[1]): v for k, v in cached.items()
    }
    missing_ids = [a for a in article_ids if a not in out]
    if missing_ids:
        computed = _bulk_uncached(session, missing_ids)
        for article_id, info in computed.items():
            cache.set(
                f"lifecycle_status:{article_id}",
                info,
                ttl=LIFECYCLE_STATUS_TTL_SEC,
            )
            out[article_id] = info
    return out


__all__ = [
    "LIFECYCLE_STATUS_TTL_SEC",
    "LifecycleStatus",
    "LifecycleStatusInfo",
    "_bulk_uncached",
    "lifecycle_status_for_articles",
]
