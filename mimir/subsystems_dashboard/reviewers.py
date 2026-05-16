"""Reviewer-attestation surfaces: per-reviewer activity listing and
per-subsystem active-reviewer ranking.

Both helpers walk `article_trailers` rows (Reviewed-by / Acked-by /
Tested-by / Reported-by / Suggested-by / Co-developed-by /
Reported-and-tested-by) and shape them either as one entry per
attestation (`articles_reviewed_by`) or aggregated per reviewer
(`active_reviewers_in_subsystem`).
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.models import Inbox, Subsystem
from mimir.subsystems import SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC
from mimir.subsystems_dashboard.reads import _subsystem_path_filter_sql
from mimir.threading import ACTIVE_THREADS_CACHE_TTL_SEC, _coerce_dt


@dataclass
class ReviewerStat:
    """One reviewer's activity in a subsystem over the recent window.

    `name` is the display name from the most-recent trailer the
    reviewer authored (the address is the stable identity; the name
    can drift across messages, and "their latest" is least-surprising
    for the visible label).

    `address` is the verbatim address from the most-recent trailer
    same rationale as `name`. The render-time redaction policy
    (see `_redact_trailer_address` in `mimir.web`) consumes this and
    decides whether to show it or substitute `<hidden>` based on the
    allowlist.

    `address_normalized` is the lowercased address used for grouping
    and as the stable per-author identity (slice 3 will key URLs off
    it).

    `role_counts` is `{role: n}` summed across the window, e.g.
    `{"Reviewed-by": 12, "Acked-by": 4, "Tested-by": 1}`. `total`
    is the sum, kept separately so the template doesn't need a
    Jinja `sum` over the dict.

    `last_seen` is the most recent article date carrying any
    attestation by this reviewer, used as the secondary sort key
    (after `total`) so equally-active reviewers are ordered by
    freshness.

    Dataclass (not pydantic) for cache-encoder compatibility, see
    `mimir.cache` which round-trips registered types via
    `dataclasses.fields`.
    """
    name: str
    address: str
    address_normalized: str
    role_counts: dict[str, int]
    total: int
    last_seen: datetime


cache.register("ReviewerStat", ReviewerStat)


@dataclass
class ReviewEntry:
    """One attestation by a specific reviewer on a specific article.

    Same person under two roles on one article (Reported-by +
    Tested-by) shows as two entries; that's accurate to the source
    trailer block and useful for the per-reviewer page reading.

    `inbox_name` is the canonical inbox for the article (resolved
    via Article.canonical_inbox or falling back to any linked inbox);
    used to construct the message URL. The reviewer page itself is
    inbox-scoped, but cross-posted articles still get the right
    canonical link.
    """
    article_id: int
    message_id: str
    subject: str | None
    date: datetime | None
    role: str
    inbox_name: str


cache.register("ReviewEntry", ReviewEntry)


# Cap on per-reviewer attestation listings. 100 covers a year of
# heavy maintainer activity (Greg KH, Linus Torvalds, etc.) without
# the page growing unbounded; the cap surfaces in the truncation
# notice on the reviewer template.
REVIEWS_PER_PAGE_LIMIT = 100


def articles_reviewed_by(
    session: Session, inbox: Inbox, address_normalized: str,
    limit: int = REVIEWS_PER_PAGE_LIMIT, force: bool = False,
) -> list[ReviewEntry]:
    """All attestations by `address_normalized` on articles linked
    to `inbox`, newest-first.

    `address_normalized` is matched exactly against
    `article_trailers.address_normalized` (already lowercased at
    ingest); callers should `.lower()` before passing if the input
    came from a URL or untrusted source.

    Cached per `(inbox.name, address, limit)` for the same TTL as
    the threads helper. Cache key uses the address verbatim, it's
    already lowercased so casing collisions are impossible.
    """
    def compute() -> list[ReviewEntry]:
        # Earlier shape JOINed against a MATERIALIZE'd derived table
        # that computed MIN(inbox.name) per article across the *entire*
        # archive just to provide a fallback name when canonical_inbox_id
        # was NULL. That scanned millions of rows on a cold miss before
        # producing any output (verified via EXPLAIN QUERY PLAN).
        #
        # Replace the materialised view with a correlated subquery in
        # the COALESCE: it fires per result row, bounded by LIMIT, and
        # only matters when canon.name is NULL (which the cache miss
        # tells us is the minority case once the canonical backfill has
        # run). Each firing is a tight index lookup keyed on
        # article_lists.article_id (composite PK prefix). Preserves the
        # same alphabetical-first fallback the rest of the codebase
        # uses (see `_canonical_inbox_name` in mimir.web.urls). Plan
        # pinned in test_articles_reviewed_by_plan_drops_materialize.
        rows = session.execute(
            text(
                """
                SELECT a.id AS article_id, a.message_id, a.subject,
                       a.date AS art_date, t.role,
                       COALESCE(
                           canon.name,
                           (SELECT MIN(i.name)
                            FROM article_lists al2
                            JOIN inboxes i ON i.id = al2.inbox_id
                            WHERE al2.article_id = a.id)
                       ) AS inbox_name
                FROM article_trailers t
                JOIN articles a ON a.id = t.article_id
                JOIN article_lists al ON al.article_id = a.id
                LEFT JOIN inboxes canon
                    ON canon.id = a.canonical_inbox_id
                WHERE al.inbox_id = :inbox_id
                  AND t.address_normalized = :addr
                ORDER BY a.date DESC
                LIMIT :limit
                """
            ),
            {
                "inbox_id": inbox.id,
                "addr": address_normalized,
                "limit": limit,
            },
        ).all()
        return [
            ReviewEntry(
                article_id=r.article_id,
                message_id=r.message_id,
                subject=r.subject,
                date=_coerce_dt(r.art_date) if r.art_date else None,
                role=r.role,
                inbox_name=r.inbox_name,
            )
            for r in rows
        ]

    return cache.get_or_compute(
        session,
        f"articles_reviewed_by:{inbox.name}:{address_normalized}:{limit}",
        ACTIVE_THREADS_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def active_reviewers_in_subsystem(
    session: Session, inbox: Inbox, subsystem: Subsystem,
    days: int = 30, limit: int = 10, force: bool = False,
) -> list[ReviewerStat]:
    """Most-active reviewers in `inbox` over the last `days` days
    among messages whose paths match `subsystem`'s F: globs (minus
    X: vetoes).

    "Reviewer" here is anyone whose address appears on an indexed
    review-attestation trailer (`Reviewed-by` / `Acked-by` /
    `Tested-by` / `Reported-by` / `Suggested-by` /
    `Co-developed-by` / `Reported-and-tested-by`). Each trailer row
    is one attestation, counted once per role per article.

    Why a 30-day window (not 7d like `active_threads_in_subsystem`):
    review cadence is slower than discussion cadence, a maintainer
    who reviews two patches a week would render zero or one entries
    in a 7-day window, which is too lossy to rank usefully. 30 days
    is roughly one release-cycle's worth of activity.

    Returns an empty list when the subsystem has no supported globs
    (slice 1/2 ignores wildcard rules) or the window has no
    attestations. Cached per `(inbox, subsystem, days, limit)` for
    the same TTL as the threads helper.
    """
    def compute() -> list[ReviewerStat]:
        path_filter = _subsystem_path_filter_sql(subsystem, prefix="arss")
        if path_filter is None:
            return []
        path_sql, path_params = path_filter
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        # Pull every matching trailer row for the window. ORDER BY
        # date DESC so the in-Python aggregator sees the freshest
        # attestation per reviewer first; that's the row whose
        # `name` / `address` we keep as the display surface.
        rows = session.execute(
            text(
                f"""
                SELECT t.role, t.name, t.address, t.address_normalized,
                       a.date AS art_date
                FROM article_trailers t
                JOIN articles a ON a.id = t.article_id
                JOIN article_lists al ON al.article_id = a.id
                WHERE al.inbox_id = :inbox_id
                  AND a.date >= :start
                  AND a.id IN ({path_sql})
                ORDER BY a.date DESC
                """
            ),
            {
                "inbox_id": inbox.id,
                "start": start.isoformat(),
                **path_params,
            },
        ).all()

        agg: dict[str, dict] = {}
        for r in rows:
            key = r.address_normalized
            entry = agg.get(key)
            if entry is None:
                # First (= most recent) row for this reviewer wins
                # the display name and verbatim address.
                entry = {
                    "name": r.name or "",
                    "address": r.address,
                    "address_normalized": key,
                    "role_counts": defaultdict(int),
                    "last_seen": _coerce_dt(r.art_date),
                }
                agg[key] = entry
            entry["role_counts"][r.role] += 1
            # Older rows can't lift `last_seen` (we sorted DESC) so
            # only the first-seen value matters; no max() needed.

        stats = [
            ReviewerStat(
                name=e["name"],
                address=e["address"],
                address_normalized=e["address_normalized"],
                role_counts=dict(e["role_counts"]),
                total=sum(e["role_counts"].values()),
                last_seen=e["last_seen"],
            )
            for e in agg.values()
        ]
        # Primary: total attestations DESC; tiebreak: last_seen DESC
        # so equally-active reviewers are ordered by freshness.
        stats.sort(key=lambda s: (-s.total, -s.last_seen.timestamp()))
        return stats[:limit]

    return cache.get_or_compute(
        session,
        f"active_reviewers_in_subsystem:{inbox.name}:{subsystem.id}:{days}:{limit}",
        SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC,
        compute,
        force=force,
    )
