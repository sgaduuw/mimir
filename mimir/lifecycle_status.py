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
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.datetime_utils import aware_utc as _aware_utc


def _coerce_dt(value) -> datetime | None:
    """text() raw SQL bypasses SQLAlchemy type coercion, so DateTime
    columns come back as ISO strings (the CTE round-trips
    `articles.date` and `mainline_commits.committed_at` through
    SQLite's TEXT storage). Parse back to datetime; tz normalization
    happens at the comparison boundary via `_aware_utc`.
    """
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except TypeError, ValueError:
        return None


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
# Recursive-CTE shape: roots(leaf, ...) walks UP from each leaf via
# thread_parent so root_per_leaf picks the topmost ancestor; then
# descendants(...) walks DOWN from each root and thread_dates takes
# the MAX(date) over the subtree, excluding the leaf article itself.
# `last_thread_date` ends up NULL when an article has no replies.
#
# `mc` carries the in_linus count + linus_sha/at AND the earliest non-
# linus tree + sha/at for the LANDED/QUEUED tooltip leading lines.
#
# `rc` is the per-article review-trailer count (total + maintainer
# subset, joined on LOWER(sm.address) = t.address_normalized). The
# `at_review` CTE is the cheap "any review trailer exists" predicate
# that still drives the REVIEWED state classification (kept distinct
# from rc so the state derivation doesn't pay the COUNT cost when
# rc is None).
#
# `sup` keeps the existing CAST-to-INTEGER comparison for patch_series_
# version so v10 > v9 (test_superseded_handles_double_digit_versions).
# Adds sup_by_version + sup_by_date for the SUPERSEDED tooltip's
# leading line.
_BULK_SQL = text("""
WITH RECURSIVE
roots(leaf, message_id, thread_parent, depth) AS (
    SELECT id, message_id, thread_parent, 0
      FROM articles WHERE id IN :ids
    UNION ALL
    SELECT r.leaf, p.message_id, p.thread_parent, r.depth + 1
      FROM roots r
      JOIN articles p ON p.message_id = r.thread_parent
     WHERE r.thread_parent IS NOT NULL AND r.depth < 1000
),
root_per_leaf AS (
    SELECT leaf, message_id AS root_msgid
      FROM (
          SELECT leaf, message_id,
                 ROW_NUMBER() OVER (PARTITION BY leaf ORDER BY depth DESC) AS rn
            FROM roots
      )
     WHERE rn = 1
),
descendants(root_msgid, message_id, thread_parent, depth) AS (
    SELECT root_msgid, root_msgid, NULL, 0 FROM root_per_leaf
    UNION
    SELECT d.root_msgid, c.message_id, c.thread_parent, d.depth + 1
      FROM descendants d
      JOIN articles c ON c.thread_parent = d.message_id
     WHERE d.depth < 1000
),
thread_dates AS (
    SELECT r.leaf, MAX(a.date) AS last_thread_date
      FROM root_per_leaf r
      JOIN descendants d ON d.root_msgid = r.root_msgid
      JOIN articles a ON a.message_id = d.message_id
     WHERE a.message_id != (SELECT message_id FROM articles WHERE id = r.leaf)
     GROUP BY r.leaf
),
mc AS (
    SELECT a.id AS article_id,
           SUM(CASE WHEN c.tree_name = 'linus' THEN 1 ELSE 0 END) AS in_linus,
           MAX(CASE WHEN c.tree_name = 'linus' THEN c.commit_sha END) AS linus_sha,
           MAX(CASE WHEN c.tree_name = 'linus' THEN c.committed_at END) AS linus_committed_at,
           (SELECT cc.tree_name FROM mainline_commits cc
              WHERE cc.message_id = a.message_id AND cc.tree_name != 'linus'
              ORDER BY cc.committed_at ASC LIMIT 1) AS earliest_other_tree,
           (SELECT cc.commit_sha FROM mainline_commits cc
              WHERE cc.message_id = a.message_id AND cc.tree_name != 'linus'
              ORDER BY cc.committed_at ASC LIMIT 1) AS earliest_other_sha,
           (SELECT cc.committed_at FROM mainline_commits cc
              WHERE cc.message_id = a.message_id AND cc.tree_name != 'linus'
              ORDER BY cc.committed_at ASC LIMIT 1) AS earliest_other_at
      FROM articles a
      LEFT JOIN mainline_commits c ON c.message_id = a.message_id
     WHERE a.id IN :ids
     GROUP BY a.id
),
rc AS (
    SELECT t.article_id,
           COUNT(*) AS review_total,
           SUM(CASE WHEN sm.address IS NOT NULL THEN 1 ELSE 0 END) AS review_maintainer
      FROM article_trailers t
      LEFT JOIN subsystem_maintainers sm
             ON LOWER(sm.address) = t.address_normalized
            AND sm.role IN ('M', 'R')
     WHERE t.article_id IN :ids
       AND t.role IN ('Reviewed-by', 'Acked-by', 'Tested-by')
     GROUP BY t.article_id
),
at_review AS (
    SELECT t.article_id, 1 AS has_review
      FROM article_trailers t
     WHERE t.article_id IN :ids
       AND t.role IN ('Reviewed-by', 'Acked-by', 'Tested-by')
     GROUP BY t.article_id
),
sup AS (
    SELECT a1.id AS leaf,
           MAX(a2.patch_series_version) AS sup_by_version,
           MAX(a2.date) AS sup_by_date
      FROM articles a1
      JOIN articles a2
        ON a1.patch_series_key IS NOT NULL
       AND a1.patch_series_key = a2.patch_series_key
       AND COALESCE(a1.patch_series_position, 0) =
           COALESCE(a2.patch_series_position, 0)
       AND CAST(a2.patch_series_version AS INTEGER) >
           CAST(a1.patch_series_version AS INTEGER)
     WHERE a1.id IN :ids
     GROUP BY a1.id
)
SELECT a.id,
       a.date AS article_date,
       COALESCE(mc.in_linus, 0) AS in_linus,
       mc.linus_sha,
       mc.linus_committed_at,
       mc.earliest_other_tree AS earliest_other,
       mc.earliest_other_sha,
       mc.earliest_other_at,
       (at_review.has_review IS NOT NULL) AS has_review,
       (sup.leaf IS NOT NULL) AS superseded,
       sup.sup_by_version,
       sup.sup_by_date,
       COALESCE(rc.review_total, 0) AS review_total,
       COALESCE(rc.review_maintainer, 0) AS review_maintainer,
       thread_dates.last_thread_date
  FROM articles a
  LEFT JOIN mc ON mc.article_id = a.id
  LEFT JOIN at_review ON at_review.article_id = a.id
  LEFT JOIN sup ON sup.leaf = a.id
  LEFT JOIN rc ON rc.article_id = a.id
  LEFT JOIN thread_dates ON thread_dates.leaf = a.id
 WHERE a.id IN :ids
""").bindparams(bindparam("ids", expanding=True))


# Per-article reviewer names for the tooltip. Kept separate from
# `_BULK_SQL` so the main fetcher's plan stays clean (only the
# tooltip renderer consumes the names). Order by `t.id` so the
# emission order is stable + matches trailer insertion order
# (article_trailers has no `created_at`; PK id ascends in insert
# order). `_format_tooltip` re-orders maintainers-first at render.
_REVIEWER_NAMES_SQL = text("""
SELECT t.article_id,
       t.name,
       (sm.address IS NOT NULL) AS is_maintainer
  FROM article_trailers t
  LEFT JOIN subsystem_maintainers sm
         ON LOWER(sm.address) = t.address_normalized
        AND sm.role IN ('M', 'R')
 WHERE t.article_id IN :ids
   AND t.role IN ('Reviewed-by', 'Acked-by', 'Tested-by')
 ORDER BY t.article_id, t.id ASC
""").bindparams(bindparam("ids", expanding=True))


def _format_pill_label(state: LifecycleStatus, tree: str | None) -> str:
    """Build the uppercase pill label for a state. QUEUED reads
    `IN <TREE>` (kebab-case slug uppercased). Other states use the
    state name verbatim."""
    if state == LifecycleStatus.QUEUED:
        tree_label = (tree or "").upper()
        return f"IN {tree_label}".strip()
    return state.value.upper()


def _format_tooltip(
    *,
    state: LifecycleStatus,
    linus_sha: str | None,
    linus_committed_at: datetime | None,
    earliest_other_sha: str | None,
    earliest_other_at: datetime | None,
    superseded_by_version: str | None,
    superseded_by_posted_at: datetime | None,
    reviewers: list[tuple[str, bool]],
) -> str | None:
    """Per-state tooltip text for the lifecycle pill. None when
    state is PENDING (no pill rendered, no tooltip).

    Maintainers ordered first within the reviewer list (preserving
    input order within each group); each line carries an `M `
    prefix for maintainers, plain name otherwise.

    Leading line by state:
      - LANDED: `<linus_sha[:12]> · <linus_at YYYY-MM-DD HH:MM>`
      - QUEUED: `<earliest_other_sha[:12]> · <earliest_other_at>`
      - REVIEWED: (none; names start on line 1)
      - SUPERSEDED: `Superseded by v<N> posted <date YYYY-MM-DD>`
      - PENDING: returns None
    """
    if state == LifecycleStatus.PENDING:
        return None

    lines: list[str] = []
    if state == LifecycleStatus.LANDED and linus_sha and linus_committed_at:
        lines.append(
            f"{linus_sha[:12]} · {linus_committed_at.strftime('%Y-%m-%d %H:%M')}"
        )
    elif state == LifecycleStatus.QUEUED and earliest_other_sha and earliest_other_at:
        lines.append(
            f"{earliest_other_sha[:12]} · {earliest_other_at.strftime('%Y-%m-%d %H:%M')}"
        )
    elif state == LifecycleStatus.SUPERSEDED:
        if superseded_by_version and superseded_by_posted_at:
            lines.append(
                f"Superseded by v{superseded_by_version} "
                f"posted {superseded_by_posted_at.strftime('%Y-%m-%d')}"
            )
        else:
            lines.append("Superseded")

    # Reviewer names: dedup by name (a single maintainer filing
    # Reviewed-by once per patch in a multi-patch series surfaces
    # as N copies of the same name in `reviewers`; the tooltip
    # should show one line per human, not one per trailer row).
    # First-seen wins; if the same name appears both as maintainer
    # and non-maintainer (parser quirk), the maintainer-flagged
    # entry takes precedence so we don't lose the `M ` prefix.
    seen_as_maintainer: set[str] = set()
    seen_as_other: set[str] = set()
    maintainers: list[str] = []
    others: list[str] = []
    for name, is_m in reviewers:
        if is_m:
            if name not in seen_as_maintainer:
                seen_as_maintainer.add(name)
                maintainers.append(name)
        else:
            # Skip if we already accepted this name as a maintainer;
            # otherwise add as non-maintainer (first-seen wins).
            if name in seen_as_maintainer:
                continue
            if name not in seen_as_other:
                seen_as_other.add(name)
                others.append(name)

    for name in maintainers:
        lines.append(f"M {name}")
    for name in others:
        lines.append(name)

    return "\n".join(lines) if lines else None


def _format_count_suffix(total: int, maintainer_count: int) -> str | None:
    """Build the inline count suffix `: N (XM)` for the pill, or
    None when the count is zero (`LANDED` with no reviews is just
    `LANDED`, no suffix)."""
    if total == 0:
        return None
    return f": {total} ({maintainer_count}M)"


def _bulk_uncached(
    session: Session,
    article_ids: list[int],
) -> dict[int, LifecycleStatusInfo]:
    """Run the bulk SQL + reviewer-names query, derive the 5-state
    classification, and pre-compute all display fields per row.
    Pure compute; no cache. Called by `lifecycle_status_for_
    articles` on cache misses and by the query-plan-pin test
    directly."""
    rows = session.execute(_BULK_SQL, {"ids": article_ids}).all()
    name_rows = session.execute(_REVIEWER_NAMES_SQL, {"ids": article_ids}).all()

    # Index reviewer names per article: [(name, is_maintainer), ...]
    # in trailer-insertion order (PK id ascending). `_format_tooltip`
    # re-sorts maintainers-first at render time.
    names_by_article: dict[int, list[tuple[str, bool]]] = {}
    for nr in name_rows:
        names_by_article.setdefault(nr.article_id, []).append(
            (nr.name or "", bool(nr.is_maintainer))
        )

    # `_activity_heat` lives on patch_state to keep the heat-bucket
    # definition next to its sibling `_days_since_last_reply` helper.
    # Deferred function-scope import dodges a circular module-load
    # path through cache.register at import time.
    from mimir.patch_state import _activity_heat

    now = datetime.now(timezone.utc)
    out: dict[int, LifecycleStatusInfo] = {}
    for row in rows:
        # State classification: same five-way priority as before
        # (LANDED > SUPERSEDED > QUEUED > REVIEWED > PENDING).
        if row.in_linus and row.in_linus > 0:
            state = LifecycleStatus.LANDED
            tree = None
        elif row.superseded:
            state = LifecycleStatus.SUPERSEDED
            tree = None
        elif row.earliest_other:
            state = LifecycleStatus.QUEUED
            tree = row.earliest_other
        elif row.has_review:
            state = LifecycleStatus.REVIEWED
            tree = None
        else:
            state = LifecycleStatus.PENDING
            tree = None

        # Thread-activity → heat. Compute days from the SQL-derived
        # `last_thread_date` (NULL when the article has no replies).
        # Treat last_thread_date <= article_date as "no replies"
        # (e.g. a malformed thread where a parent post-dates a
        # child); the heat bucket then collapses to dormant.
        last_at = _coerce_dt(row.last_thread_date)
        article_at = _coerce_dt(row.article_date)
        days: int | None
        if last_at is None or article_at is None:
            days = None
        else:
            last_at_aware = _aware_utc(last_at)
            article_at_aware = _aware_utc(article_at)
            if last_at_aware <= article_at_aware:
                days = None
            else:
                days = max(0, (now - last_at_aware).days)
        heat, detail = _activity_heat(days)

        pill_label = _format_pill_label(state, tree)
        count_suffix = _format_count_suffix(
            row.review_total or 0,
            row.review_maintainer or 0,
        )
        # `sup_by_date` and the committed_at fields likewise need
        # coercion: they're carried through CTE projections + outer
        # SELECTs, which SQLAlchemy can't type-annotate for raw text().
        tooltip = _format_tooltip(
            state=state,
            linus_sha=row.linus_sha,
            linus_committed_at=_coerce_dt(row.linus_committed_at),
            earliest_other_sha=row.earliest_other_sha,
            earliest_other_at=_coerce_dt(row.earliest_other_at),
            superseded_by_version=row.sup_by_version,
            superseded_by_posted_at=_coerce_dt(row.sup_by_date),
            reviewers=names_by_article.get(row.id, []),
        )

        out[row.id] = LifecycleStatusInfo(
            state_value=state.value,
            tree=tree,
            activity_heat=heat,
            activity_detail=detail,
            pill_label=pill_label,
            count_suffix=count_suffix,
            tooltip=tooltip,
        )
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
    "_format_count_suffix",
    "_format_pill_label",
    "_format_tooltip",
    "lifecycle_status_for_articles",
]
