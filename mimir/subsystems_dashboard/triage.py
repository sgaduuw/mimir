"""Per-subsystem triage queues: "needs attention" + "quiet for 30+
days." Sibling of `reads.py`, separate file because the queries are
structurally different (NOT EXISTS predicates against article_trailers
+ mainline_commits + same-series later versions) and the maintainer-
pickup exclusion is a triage-only concern.

Both helpers ORDER BY `articles.date ASC` so the oldest concerning
entries surface first; a maintainer scanning the triage queue cares
about "what's been sitting longest", not "what's most recent." Both
filter through the shared `_subsystem_path_filter_sql` UNION-of-seeks
so the path-side cost is the same as the other per-subsystem
helpers. Both cached for `SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC` to
ride out repeat dashboard hits within a refresh window.
"""

from datetime import datetime, timedelta, timezone
from typing import Callable

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.config import settings
from mimir.models import (
    ArticleTrailer,
    Inbox,
    Subsystem,
    SubsystemMaintainer,
)
from mimir.subsystems import SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC
from mimir.subsystems_dashboard._path_filter import (
    _subsystem_path_filter_exists_sql,
)
from mimir.threading import _coerce_dt


class PatchAttention(BaseModel):
    """One entry in a triage queue (needs-attention or quiet).
    `trailer_summary` is non-empty for needs-attention (e.g.
    "3 Reviewed-by, 1 Tested-by") and empty for the quiet list
    (which by definition has no trailers)."""

    article_id: int
    message_id: str
    subject: str | None
    author: str | None
    date: datetime | None
    inbox_name: str
    trailer_summary: str = ""


cache.register("PatchAttention", PatchAttention)


# Trailer roles that count as "engagement" for the needs-attention
# match (≥1 of these and the patch qualifies, modulo the
# maintainer-pickup exclusion). Mirrors the review-trailer set from
# the patch-state card.
_REVIEW_TRAILER_ROLES = ("Reviewed-by", "Acked-by", "Tested-by")

# Trailer role that signals a maintainer pickup. Distinct from the
# review-trailer set above: a maintainer's Reviewed-by is feedback,
# but a maintainer's Acked-by is "I'll merge this." Exclude only on
# Acked-by; a patch with just maintainer-Reviewed-by is still
# arguably needs-attention until the maintainer Acks it.
_MAINTAINER_PICKUP_ROLE = "Acked-by"


def _filter_maintainer_acked(
    session: Session,
    article_ids: list[int],
) -> set[int]:
    """Return the subset of `article_ids` whose `article_trailers`
    include an Acked-by from any address that's also a maintainer
    in `subsystem_maintainers` (`M` or `R`). The needs-attention
    list excludes these on the principle that a maintainer Ack is
    the pickup signal, even when the Ack came from a maintainer of
    a different subsystem the patch touches.

    Approximation: cross-references against `subsystem_maintainers`
    globally rather than path-resolving each article back to its
    subsystem set. Random reviewers don't appear in
    `subsystem_maintainers`, and a maintainer-Ack from any
    subsystem usually does mean pickup, so the looser global check
    matches the operational intent. The strict per-article path-
    walk variant is `subsystems_for_article(...)` but that's a
    Python-side glob match against ~10k rules per article, an N+1
    waiting to happen for a triage list.
    """
    if not article_ids:
        return set()
    # Single batched query joining trailers to maintainer addresses.
    # `address_normalized` on the trailer side is already lowercased
    # at ingest; the maintainer side stores `address` as the
    # MAINTAINERS-file source string, so lowercase it in SQL for the
    # join to be index-friendly on the trailer side and tolerant of
    # case drift on the maintainer side.
    rows = session.execute(
        select(ArticleTrailer.article_id)
        .distinct()
        .join(
            SubsystemMaintainer,
            SubsystemMaintainer.address.collate("NOCASE")
            == ArticleTrailer.address_normalized,
        )
        .where(
            ArticleTrailer.role == _MAINTAINER_PICKUP_ROLE,
            SubsystemMaintainer.role.in_(("M", "R")),
            ArticleTrailer.article_id.in_(article_ids),
        )
    ).scalars()
    return set(rows)


def _trailer_summary(
    session: Session,
    article_ids: list[int],
) -> dict[int, str]:
    """Build a human-readable per-article trailer count summary
    keyed by article_id. Empty articles produce empty strings.
    One query, cheap, used only on the small needs-attention list."""
    if not article_ids:
        return {}
    rows = session.execute(
        select(
            ArticleTrailer.article_id,
            ArticleTrailer.role,
        ).where(
            ArticleTrailer.role.in_(_REVIEW_TRAILER_ROLES),
            ArticleTrailer.article_id.in_(article_ids),
        )
    ).all()
    by_article: dict[int, dict[str, int]] = {}
    for art_id, role in rows:
        by_article.setdefault(art_id, {})[role] = (
            by_article.setdefault(art_id, {}).get(role, 0) + 1
        )
    out: dict[int, str] = {}
    for art_id, counts in by_article.items():
        # Stable order matches the trailer-canon order on the patch-
        # state card.
        pieces = []
        for role in _REVIEW_TRAILER_ROLES:
            n = counts.get(role, 0)
            if n:
                pieces.append(f"{n} {role}")
        out[art_id] = ", ".join(pieces)
    return out


def _candidate_query_needs_attention(
    inbox: Inbox,
    subsystem: Subsystem,
    cutoff_date: datetime,
    limit: int,
) -> tuple[str, dict] | None:
    """SQL for needs-attention candidates (before the maintainer-Ack
    post-filter). Returns `(sql, params)` or `None` when the
    subsystem has no matchable paths.

    Shape: walk `ix_articles_date` ASC over a bounded date range,
    predicate-test per article, stop at `overfetch` matches.
    EXISTS-shaped path filter so the planner doesn't materialise
    the full subsystem's article_id set before LIMIT. The
    bounded date range (`min_date <= a.date <= cutoff_date`) is
    load-bearing for the cold-miss latency: unbounded walks scan
    the full date index back to the dawn of lkml (~6M rows),
    bounded to the triage window it's a few hundred thousand.
    """
    path_filter = _subsystem_path_filter_exists_sql(subsystem, prefix="na")
    if path_filter is None:
        return None
    path_predicate, path_params = path_filter
    # Over-fetch by 2x so the maintainer-Ack post-filter can drop
    # some without starving the rendered list. A constant 2x is
    # cheap (LIMIT 20 instead of LIMIT 10) and beats a second SQL
    # round-trip to backfill.
    overfetch = max(limit * 2, limit + 5)
    sql = f"""
        SELECT a.id AS article_id, a.message_id, a.subject,
               a.author, a.date AS art_date
        FROM articles a
        WHERE a.date BETWEEN :min_date AND :cutoff_date
          AND EXISTS (
              SELECT 1 FROM article_lists al
              WHERE al.article_id = a.id
                AND al.inbox_id = :inbox_id
          )
          AND {path_predicate}
          AND EXISTS (
              SELECT 1 FROM article_trailers t
              WHERE t.article_id = a.id
                AND t.role IN ('Reviewed-by', 'Acked-by', 'Tested-by')
          )
          AND NOT EXISTS (
              SELECT 1 FROM mainline_commits mc
              WHERE mc.message_id = a.message_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM articles later
              WHERE later.patch_series_key = a.patch_series_key
                AND a.patch_series_key IS NOT NULL
                AND later.id != a.id
                AND later.date > a.date
          )
        ORDER BY a.date ASC
        LIMIT :overfetch
    """
    return sql, {
        "inbox_id": inbox.id,
        "cutoff_date": cutoff_date,
        "min_date": cutoff_date
        - timedelta(
            days=settings.subsystem_triage_max_age_days,
        ),
        "overfetch": overfetch,
        **path_params,
    }


def _candidate_query_quiet(
    inbox: Inbox,
    subsystem: Subsystem,
    cutoff_date: datetime,
    limit: int,
) -> tuple[str, dict] | None:
    """SQL for the quiet list. Patches with no engagement: no
    trailers AND no replies. Returns `(sql, params)` or `None`.
    Same walk-date-ASC + per-row EXISTS shape as
    `_candidate_query_needs_attention`."""
    path_filter = _subsystem_path_filter_exists_sql(subsystem, prefix="qt")
    if path_filter is None:
        return None
    path_predicate, path_params = path_filter
    sql = f"""
        SELECT a.id AS article_id, a.message_id, a.subject,
               a.author, a.date AS art_date
        FROM articles a
        WHERE a.date BETWEEN :min_date AND :cutoff_date
          AND EXISTS (
              SELECT 1 FROM article_lists al
              WHERE al.article_id = a.id
                AND al.inbox_id = :inbox_id
          )
          AND {path_predicate}
          AND NOT EXISTS (
              SELECT 1 FROM article_trailers t
              WHERE t.article_id = a.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM articles child
              WHERE child.thread_parent = a.message_id
          )
        ORDER BY a.date ASC
        LIMIT :limit
    """
    return sql, {
        "inbox_id": inbox.id,
        "cutoff_date": cutoff_date,
        "min_date": cutoff_date
        - timedelta(
            days=settings.subsystem_triage_max_age_days,
        ),
        "limit": limit,
        **path_params,
    }


def _run_triage(
    session: Session,
    inbox: Inbox,
    subsystem: Subsystem,
    cutoff_date: datetime,
    limit: int,
    build_query: Callable[
        [Inbox, Subsystem, datetime, int],
        tuple[str, dict] | None,
    ],
    *,
    drop_maintainer_acked: bool,
    include_trailer_summary: bool,
) -> list[PatchAttention]:
    """Shared post-processing across the two triage queries: run
    the candidate SELECT, optionally drop maintainer-Acked rows,
    optionally annotate with trailer summary, build PatchAttention
    rows truncated to `limit`."""
    built = build_query(inbox, subsystem, cutoff_date, limit)
    if built is None:
        return []
    sql, params = built
    rows = session.execute(text(sql), params).all()
    if not rows:
        return []
    candidate_ids = [r.article_id for r in rows]

    drop_ids: set[int] = set()
    if drop_maintainer_acked:
        drop_ids = _filter_maintainer_acked(session, candidate_ids)

    summaries: dict[int, str] = {}
    if include_trailer_summary:
        kept_ids = [aid for aid in candidate_ids if aid not in drop_ids]
        summaries = _trailer_summary(session, kept_ids)

    out: list[PatchAttention] = []
    for r in rows:
        if r.article_id in drop_ids:
            continue
        out.append(
            PatchAttention(
                article_id=r.article_id,
                message_id=r.message_id,
                subject=r.subject,
                author=r.author,
                date=_coerce_dt(r.art_date),
                inbox_name=inbox.name,
                trailer_summary=summaries.get(r.article_id, ""),
            )
        )
        if len(out) >= limit:
            break
    return out


def needs_attention_patches_in_subsystem(
    session: Session,
    inbox: Inbox,
    subsystem: Subsystem,
    days: int | None = None,
    limit: int = 10,
    *,
    force: bool = False,
) -> list[PatchAttention]:
    """Patches that have gathered review trailers but haven't
    landed in mainline, haven't been superseded by a later
    revision, aren't already Acked by a (subsystem) maintainer,
    and have been waiting at least `days` days. Oldest first.

    `days=None` defers to `Settings.subsystem_needs_attention_days`
    (default 14)."""
    age_days = days if days is not None else settings.subsystem_needs_attention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=age_days)

    def compute() -> list[PatchAttention]:
        return _run_triage(
            session,
            inbox,
            subsystem,
            cutoff,
            limit,
            _candidate_query_needs_attention,
            drop_maintainer_acked=True,
            include_trailer_summary=True,
        )

    return cache.get_or_compute(
        session,
        f"needs_attention:{inbox.name}:{subsystem.id}:{age_days}:{limit}",
        SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def quiet_patches_in_subsystem(
    session: Session,
    inbox: Inbox,
    subsystem: Subsystem,
    days: int | None = None,
    limit: int = 10,
    *,
    force: bool = False,
) -> list[PatchAttention]:
    """Patches with no engagement at all: no review trailers and
    no replies. At least `days` days old. Oldest first.

    `days=None` defers to `Settings.subsystem_quiet_days` (default
    30). Wording in the rendered surface is "Quiet for 30+ days",
    not "Stalled": authors who didn't get review are often just
    busy or working on something else, not stalled."""
    age_days = days if days is not None else settings.subsystem_quiet_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=age_days)

    def compute() -> list[PatchAttention]:
        return _run_triage(
            session,
            inbox,
            subsystem,
            cutoff,
            limit,
            _candidate_query_quiet,
            drop_maintainer_acked=False,
            include_trailer_summary=False,
        )

    return cache.get_or_compute(
        session,
        f"quiet_patches:{inbox.name}:{subsystem.id}:{age_days}:{limit}",
        SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC,
        compute,
        force=force,
    )


__all__ = [
    "PatchAttention",
    "needs_attention_patches_in_subsystem",
    "quiet_patches_in_subsystem",
]
