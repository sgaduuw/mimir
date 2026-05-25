"""Cross-inbox "most active subsystems" surfaces (the front-page
teaser and the per-inbox dashboard widget).

The per-inbox helper is the heavy hitter: one bulk SQL + an
inverted-index walk over MAINTAINERS rules + a single Python pass
that aggregates per-subsystem counts and per-day buckets for the
inline sparklines. The global helper composes per-inbox results.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_cls, datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from mimir import cache
from mimir.dashboard import DailyVolume
from mimir.models import Inbox, Subsystem
from mimir.threading import _coerce_dt


@dataclass
class SubsystemActivity:
    """One row in the "most active subsystems" surface.

    `inbox_name` carries the per-row inbox attribution: the inbox
    whose `/<inbox>/subsystem/<name>/` page the row's link should
    target. For the per-inbox helper that's always the calling
    inbox; for the cross-inbox helper it's the inbox where this
    subsystem saw the most activity in the window, so the user
    lands on the busiest variant.

    `name` is the stored MAINTAINERS-verbatim casing
    (e.g. "BCACHEFS"). The dashboard route lowercases the URL
    component on emit; the rendered chip keeps the upstream
    casing so it matches what readers see elsewhere in the UI.

    `maintainer_name` is the display name of the first `M:` row
    in `subsystem_maintainers` for this subsystem. Empty string
    when MAINTAINERS lists no maintainer (rare; mostly Orphan
    sections). `multiple_maintainers` distinguishes single-name
    from "et al." rendering on the card.

    `status` is the verbatim `S:` field
    (`Supported` / `Maintained` / `Odd Fixes` / `Orphan` /
    `Obsolete`); the front-page card only surfaces it when it
    differs from the default `Maintained` (most subsystems sit
    at that value, so a badge on every card would just be noise).

    `spark` is a 7-day daily-volume series for this subsystem on
    `inbox_name`. Drives the inline sparkline on the front-page
    card. None when the subsystem has no supported globs (the
    `daily_volume_in_subsystem` helper still returns a zero-
    filled series in that case but we skip it on the cards to
    avoid rendering a flat bar row for every wildcard-only
    subsystem).

    Dataclass (not pydantic) for `mimir.cache` round-trip
    compatibility, matching the project convention for cached
    value types."""

    id: int
    name: str
    inbox_name: str
    message_count: int
    last_activity: datetime
    maintainer_name: str = ""
    multiple_maintainers: bool = False
    status: str | None = None
    spark: DailyVolume | None = None


cache.register("SubsystemActivity", SubsystemActivity)


# Cache TTL for the "most active subsystems" surfaces. Must exceed
# the warm-cache cycle time, otherwise the row expires faster than
# warm-cache can refresh it and request-path callers fall through to
# a cold-compute path that is multi-second on the per-inbox version
# and multi-minute on the cross-inbox version. On the production
# 203-inbox corpus warm-cache takes ~10 min per cycle, so the prior
# 5 min TTL had the row expired for ~4-5 min of every cycle; the
# front page's meta-index serialised on that recompute and tripped
# Cloudflare's 100 s gateway timeout (HTTP 524). 1 h covers any
# plausible warm-cache cycle time with comfortable margin; the
# `compute_on_miss=False` request-path guard below removes the
# request-path-recompute footgun even when this TTL is somehow
# exceeded. The "freshness" trade-off is acceptable: a 1 h cache
# of "what's hot in the last 7 days" lags by ~14% at worst, and
# warm-cache refreshes the row every ~10 min in practice.
MOST_ACTIVE_SUBSYSTEMS_CACHE_TTL_SEC = 3600


# How many ranked subsystems we cache per (inbox, days) tuple. Callers
# pass any `limit` ≤ this and slice from the cached list. 100 covers
# every current surface (front-page top-12, inbox dashboard top-10,
# the global aggregator's `limit*3` hedge) without recomputing per
# caller, which was the v1.19.2 warm-cache hot spot: each inbox was
# computing the same expensive aggregation three times for three
# distinct limit suffixes (10, 30, 36).
MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP = 100


def most_active_subsystems_in_inbox(
    session: Session,
    inbox: Inbox,
    days: int = 7,
    limit: int = 10,
    force: bool = False,
    *,
    compute_on_miss: bool = True,
) -> list[SubsystemActivity]:
    """Top-N subsystems by message volume in `inbox` over the
    recent window. Powers the "Most active subsystems" widget on
    the per-inbox dashboard.

    Thin slicer over `_most_active_subsystems_in_inbox_full`: the
    cached list is internally capped at
    `MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP`; every caller slices from
    that one cached payload regardless of `limit`. The cache key is
    therefore `(inbox.name, days)` only, adding `limit` to the key
    was the v1.19.2 cold-path waste (three caches for one
    aggregation).

    `compute_on_miss=True` (the default, used by warm-cache and any
    background context) falls through to a fresh compute when the
    cache is cold or expired. `compute_on_miss=False` (the
    request-path posture, per `mimir/web/routes/dashboards.py`)
    returns `[]` on a cache miss instead of blocking the request
    on a seconds-to-minutes recompute. The widget renders empty
    during the brief window between TTL expiry and the next warm-
    cache refresh; the alternative was the 1.36.0-era 524 timeout
    when a single cold render serialised gunicorn workers behind
    the per-inbox subsystem aggregation.
    """
    full = _most_active_subsystems_in_inbox_full(
        session,
        inbox,
        days=days,
        force=force,
        compute_on_miss=compute_on_miss,
    )
    return full[:limit]


def _most_active_subsystems_in_inbox_full(
    session: Session,
    inbox: Inbox,
    days: int = 7,
    force: bool = False,
    *,
    compute_on_miss: bool = True,
) -> list[SubsystemActivity]:
    """Cached full ranked list (top `MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP`)
    for `(inbox, days)`. The public `most_active_subsystems_in_inbox`
    slices this; callers should not invoke directly unless they
    truly want the full cap (e.g. the cross-inbox aggregator).

    Implementation: one bulk SQL pulls every (article_id, path, date)
    tuple in the recent window, an inverted-index walk over MAINTAINERS
    rules buckets paths into subsystems, and a single Python pass
    aggregates per-subsystem counts + per-day buckets for the inline
    sparkline.
    """

    def compute() -> list[SubsystemActivity]:
        # Calendar-day window so the inline sparkline buckets line
        # up with how `daily_volume_in_subsystem` would have queried
        # them (today + (days-1) prior calendar days). The earlier
        # rolling 168-hour window meant a message from 10am 7 days
        # ago could be in the totals but the spark would render it
        # in an off-by-one bucket. UTC because `Article.date` is the
        # public-inbox commit time in UTC; `date.today()` would
        # advance at *local* midnight and drop boundary-day articles.
        today = datetime.now(timezone.utc).date()
        start_day = today - timedelta(days=days - 1)
        start = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
        # One SQL: every (article_id, path, date) tuple for recent
        # in-window articles linked to this inbox. The date filter
        # is selective on the production corpus (millions of rows
        # → thousands of tuples) and we then match in Python.
        #
        # The earlier shape ran one COUNT per subsystem (~1500 on
        # lkml), which blew through gunicorn's worker timeout cold
        # on the 1.19.0 production deploy. The 1.19.1 hotfix
        # collapsed that to one bulk SQL + Python inverted-index
        # walk; 1.19.2 also folds the inline sparkline buckets into
        # the same loop so the surviving top-N rows don't fan out
        # one `daily_volume_in_subsystem` call each.
        rows = session.execute(
            text(
                """
                SELECT a.id AS article_id, af.path, a.date AS art_date
                FROM articles a
                JOIN article_lists al ON al.article_id = a.id
                JOIN article_files af ON af.article_id = a.id
                WHERE al.inbox_id = :inbox_id AND a.date >= :start
                """
            ),
            {"inbox_id": inbox.id, "start": start.isoformat()},
        ).all()

        # Inverted indices over MAINTAINERS subsystem path rules.
        # Built fresh per call; the subsystems table is small
        # (≈1500 rows × a handful of paths each) so this costs
        # tens of milliseconds. include_* / exclude_* mirror the
        # F: / X: rules; has_include caps the search space to
        # subsystems with at least one *supported* (non-wildcard)
        # include rule, same convention as `_subsystem_path_filter_sql`.
        subs = (
            session.execute(
                select(Subsystem).options(
                    selectinload(Subsystem.paths),
                    selectinload(Subsystem.maintainers),
                )
            )
            .scalars()
            .all()
        )
        subs_by_id: dict[int, Subsystem] = {}
        include_prefix: dict[str, set[int]] = defaultdict(set)
        include_exact: dict[str, set[int]] = defaultdict(set)
        exclude_prefix: dict[str, set[int]] = defaultdict(set)
        exclude_exact: dict[str, set[int]] = defaultdict(set)
        for sub in subs:
            subs_by_id[sub.id] = sub
            for rule in sub.paths:
                glob = rule.glob
                if glob.endswith("/"):
                    bucket = exclude_prefix if rule.is_exclude else include_prefix
                    bucket[glob[:-1]].add(sub.id)
                elif not any(c in glob for c in "*?["):
                    bucket = exclude_exact if rule.is_exclude else include_exact
                    bucket[glob].add(sub.id)
                # else: wildcard rules skipped (slice 1/2 contract;
                # the per-subsystem filter SQL skips them too).

        def matches_for(
            path: str,
            prefix_map: dict[str, set[int]],
            exact_map: dict[str, set[int]],
        ) -> set[int]:
            out: set[int] = set()
            if path in exact_map:
                out.update(exact_map[path])
            # Walk parent directories so `fs/bcachefs/super.c`
            # matches an `fs/bcachefs/` rule via its `fs/bcachefs`
            # parent. O(path_components) per lookup.
            parts = path.split("/")
            for i in range(len(parts), 0, -1):
                key = "/".join(parts[:i])
                if key in prefix_map:
                    out.update(prefix_map[key])
            return out

        # Per-subsystem aggregation. `article_set` tracks distinct
        # article_ids so a patch touching three files in the same
        # subsystem counts once, matching the prior COUNT(*) over
        # a deduplicated article_id IN (…) subquery. `day_buckets`
        # accumulates per-day distinct article sets for the inline
        # sparkline; building them here avoids a fan-out call to
        # `daily_volume_in_subsystem` per surviving top-N row,
        # which is what blew up cold in the 1.19.1 worker timeout.
        counts: dict[int, set[int]] = defaultdict(set)
        last_activity: dict[int, datetime] = {}
        day_buckets: dict[int, dict[date_cls, set[int]]] = defaultdict(
            lambda: defaultdict(set)
        )
        for r in rows:
            inc = matches_for(r.path, include_prefix, include_exact)
            if not inc:
                continue
            exc = matches_for(r.path, exclude_prefix, exclude_exact)
            matched = inc - exc
            if not matched:
                continue
            path_date = _coerce_dt(r.art_date)
            day_key = path_date.date()
            for sub_id in matched:
                counts[sub_id].add(r.article_id)
                day_buckets[sub_id][day_key].add(r.article_id)
                prev = last_activity.get(sub_id)
                if prev is None or path_date > prev:
                    last_activity[sub_id] = path_date

        # Sort + truncate to the internal cap. Callers slice further
        # for their specific `limit`; one cached payload feeds every
        # surface.
        ranked = sorted(
            counts.items(),
            key=lambda kv: (-len(kv[1]), -last_activity[kv[0]].timestamp()),
        )[:MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP]

        def _spark_for(sub_id: int) -> DailyVolume:
            """Zero-filled `days`-length series in the same shape
            `daily_volume_in_subsystem` returns. Built from the
            already-aggregated `day_buckets` for this subsystem."""
            buckets = day_buckets.get(sub_id, {})
            series = [
                (
                    start_day + timedelta(days=i),
                    len(buckets.get(start_day + timedelta(days=i), set())),
                )
                for i in range(days)
            ]
            return DailyVolume(
                days=series,
                max_count=max((c for _, c in series), default=1),
            )

        out: list[SubsystemActivity] = []
        for sub_id, article_set in ranked:
            sub = subs_by_id[sub_id]
            maintainers = sorted(
                (m for m in sub.maintainers if m.role == "M"),
                key=lambda m: m.id,
            )
            top_maintainer = maintainers[0].name if maintainers else ""
            out.append(
                SubsystemActivity(
                    id=sub_id,
                    name=sub.name,
                    inbox_name=inbox.name,
                    message_count=len(article_set),
                    last_activity=last_activity[sub_id],
                    maintainer_name=top_maintainer,
                    multiple_maintainers=len(maintainers) > 1,
                    status=sub.status,
                    spark=_spark_for(sub_id),
                )
            )
        return out

    key = f"most_active_subsystems_in_inbox:{inbox.name}:{days}"
    if not compute_on_miss:
        # Request-path posture: cache hit serves immediately, cache
        # miss serves empty so the page render never blocks on the
        # multi-second aggregation. Warm-cache keeps the row fresh.
        cached = cache.get(key)
        return cached if cached is not None else []
    return cache.get_or_compute(
        session,
        key,
        MOST_ACTIVE_SUBSYSTEMS_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def most_active_subsystems_global(
    session: Session,
    days: int = 7,
    limit: int = 10,
    force: bool = False,
    *,
    compute_on_miss: bool = True,
) -> list[SubsystemActivity]:
    """Top-N subsystems across **all** configured inboxes over the
    recent window. Powers the "Active subsystems" teaser on the
    front page (`/`).

    Thin slicer over `_most_active_subsystems_global_full`: same
    limit-less caching shape as the per-inbox helper.

    `compute_on_miss=False` (the request-path posture from the
    meta-index route) returns `[]` on cache miss rather than
    blocking on the cross-inbox aggregation. Critical here: this
    aggregator iterates every configured inbox's per-inbox
    aggregation in turn, so a cold compute on a 200-inbox corpus
    can run for minutes. A single cold front-page render under
    that posture would serialise every gunicorn worker behind the
    same compute and trip the gateway timeout (1.36.0 production
    incident). With `compute_on_miss=False`, the widget renders
    empty for the brief window between TTL expiry and the next
    warm-cache refresh, and the rest of the page renders normally.
    """
    full = _most_active_subsystems_global_full(
        session,
        days=days,
        force=force,
        compute_on_miss=compute_on_miss,
    )
    return full[:limit]


def _most_active_subsystems_global_full(
    session: Session,
    days: int = 7,
    force: bool = False,
    *,
    compute_on_miss: bool = True,
) -> list[SubsystemActivity]:
    """Cached full cross-inbox ranking (top `MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP`)
    for `days`. Each subsystem appears at most once; the row's
    `inbox_name` is the inbox where this subsystem saw the most
    activity in the window, so the chip links the reader to the
    busiest variant. Inbox-name tie-break is alphabetical for
    determinism.

    Implementation: consume each inbox's already-cached full ranked
    list (no `limit*3` hedge needed, we have the whole top-100 per
    inbox), merge by subsystem id, pick the busiest inbox per
    subsystem, re-sort globally, truncate to the internal cap.
    """

    def compute() -> list[SubsystemActivity]:
        inboxes = session.execute(select(Inbox)).scalars().all()
        agg: dict[int, dict] = {}
        for inbox in inboxes:
            # Propagate `force` so a `warm-cache --force` (or any
            # other forced global refresh) actually recomputes the
            # per-inbox rows instead of reading whatever stale value
            # the per-inbox cache last wrote. Audit (2026-05-15)
            # flagged this gap: the outer cache wrap bypasses, the
            # inner one silently doesn't.
            for row in _most_active_subsystems_in_inbox_full(
                session,
                inbox,
                days=days,
                force=force,
            ):
                entry = agg.get(row.id)
                if entry is None:
                    agg[row.id] = {
                        "name": row.name,
                        "total": row.message_count,
                        "best_count": row.message_count,
                        "best_inbox": row.inbox_name,
                        "best_last_activity": row.last_activity,
                        # Maintainer + status are subsystem-level
                        # (not per-inbox), so first-seen wins and
                        # later iterations don't override.
                        "maintainer_name": row.maintainer_name,
                        "multiple_maintainers": row.multiple_maintainers,
                        "status": row.status,
                        # Sparkline IS per-inbox; it follows the
                        # best-inbox attribution below so the card
                        # shows the sparkline for the inbox the
                        # chip links to.
                        "best_spark": row.spark,
                    }
                    continue
                entry["total"] += row.message_count
                if row.message_count > entry["best_count"] or (
                    row.message_count == entry["best_count"]
                    and row.inbox_name < entry["best_inbox"]
                ):
                    entry["best_count"] = row.message_count
                    entry["best_inbox"] = row.inbox_name
                    entry["best_last_activity"] = row.last_activity
                    entry["best_spark"] = row.spark
        out = [
            SubsystemActivity(
                id=sub_id,
                name=e["name"],
                inbox_name=e["best_inbox"],
                message_count=e["total"],
                last_activity=e["best_last_activity"],
                maintainer_name=e["maintainer_name"],
                multiple_maintainers=e["multiple_maintainers"],
                status=e["status"],
                spark=e["best_spark"],
            )
            for sub_id, e in agg.items()
        ]
        out.sort(key=lambda a: (-a.message_count, -a.last_activity.timestamp()))
        return out[:MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP]

    key = f"most_active_subsystems_global:{days}"
    if not compute_on_miss:
        cached = cache.get(key)
        return cached if cached is not None else []
    return cache.get_or_compute(
        session,
        key,
        MOST_ACTIVE_SUBSYSTEMS_CACHE_TTL_SEC,
        compute,
        force=force,
    )
