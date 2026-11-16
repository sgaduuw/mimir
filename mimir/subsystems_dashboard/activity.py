"""Cross-inbox "most active subsystems" surfaces (the front-page
teaser and the per-inbox dashboard widget).

The per-inbox helper is the heavy hitter: one bulk SQL + an
inverted-index walk over MAINTAINERS rules + a single Python pass
that aggregates per-subsystem counts. The global helper composes
per-inbox results.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, bindparam, select, text
from sqlalchemy.orm import Session, selectinload

from mimir import cache
from mimir.datetime_utils import coerce_dt
from mimir.mailmap import rules_version
from mimir.models import Inbox, Subsystem


@dataclass
class SubsystemActivity:
    """One row in the "most active subsystems" surface.

    `inbox_name` carries the per-row inbox attribution: the inbox
    whose `/<inbox>/subsystems/<name>/` page the row's link should
    target. For the per-inbox helper that's always the calling
    inbox; for the cross-inbox helper it's the inbox where this
    subsystem saw the most activity in the window, so the user
    lands on the busiest variant.

    `name` is the stored MAINTAINERS-verbatim casing
    (e.g. "BCACHEFS"). The dashboard route lowercases the URL
    component on emit, and templates show the name through the
    `subsystem_label` filter, so the chip matches the rest of the UI.

    `maintainer_name` is the display name of the first `M:` row
    in `subsystem_maintainers` for this subsystem. Empty string
    when MAINTAINERS lists no maintainer (rare; mostly Orphan
    sections). `multiple_maintainers` distinguishes single-name
    from "et al." rendering on the subsystem index.

    `status` is the verbatim `S:` field
    (`Supported` / `Maintained` / `Odd Fixes` / `Orphan` /
    `Obsolete`). Maintainer and status fields serve the per-inbox
    subsystem index; global ranking rows leave them at their defaults.

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


cache.register("SubsystemActivity", SubsystemActivity)


# One hour matches slow warming. A fresh complete sweep of 203 production
# inboxes took 46.43 s on 2026-11-16 at 10:37 UTC; requests only read the cache.
MOST_ACTIVE_SUBSYSTEMS_CACHE_TTL_SEC = 3600


# Measured 2026-11-16 at 10:37 UTC: 1,294 active subsystems across 203 inboxes.
# The complete global payload is 320,616 bytes; the largest inbox has 1,180
# active subsystems. Keep all of them, and apply display limits only at callers.


class _RulesChanged(Exception):
    """Do not publish counts against subsystem IDs from a replaced rule set."""


def most_active_subsystems_in_inbox(
    session: Session,
    inbox: Inbox,
    days: int = 7,
    limit: int | None = 10,
    force: bool = False,
    *,
    compute_on_miss: bool = True,
) -> list[SubsystemActivity]:
    """Top-N subsystems by message volume in `inbox` over the
    recent window. Powers the "Most active subsystems" widget on
    the per-inbox dashboard.

    Thin slicer over `_most_active_subsystems_in_inbox_full`: the
    complete cached list is shared regardless of display `limit`.
    Keys carry inbox, days and the rules generation so replacement IDs
    never inherit an older rule set's activity.

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
    return (full or [])[:limit]


def _most_active_subsystems_in_inbox_full(
    session: Session,
    inbox: Inbox,
    days: int = 7,
    force: bool = False,
    *,
    compute_on_miss: bool = True,
) -> list[SubsystemActivity] | None:
    """Complete cached ranking, or None when a cache-only read is cold.
    The public helper slices this for its widget; directories retain every row.

    Implementation: one bulk SQL pulls every (article_id, path, date)
    tuple in the recent window, an inverted-index walk over MAINTAINERS
    rules buckets paths into subsystems, and a single Python pass
    aggregates per-subsystem counts.
    """

    version = rules_version(session)

    def compute() -> list[SubsystemActivity]:
        # Match the dashboard's calendar-day window. UTC keeps local
        # midnight from dropping boundary-day articles early.
        today = datetime.now(UTC).date()
        start_day = today - timedelta(days=days - 1)
        start = datetime.combine(start_day, datetime.min.time(), tzinfo=UTC)
        # One SQL: every (article_id, path, date) tuple for recent
        # in-window articles linked to this inbox. The date filter
        # is selective on the production corpus (millions of rows
        # → thousands of tuples) and we then match in Python.
        #
        # The earlier shape ran one COUNT per subsystem (~1500 on
        # lkml), which blew through gunicorn's worker timeout cold
        # on the 1.19.0 production deploy. The 1.19.1 hotfix
        # collapsed that to one bulk SQL + Python inverted-index walk.
        rows = session.execute(
            text(
                """
                SELECT a.id AS article_id, af.path, a.date AS art_date
                FROM articles a
                JOIN article_lists al ON al.article_id = a.id
                JOIN article_files af ON af.article_id = a.id
                WHERE al.inbox_id = :inbox_id AND a.date >= :start
                """
            ).bindparams(bindparam("start", type_=DateTime)),
            {"inbox_id": inbox.id, "start": start},
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
                select(Subsystem)
                # A reused caller Session may retain objects from an older
                # generation even after the database rules changed.
                .execution_options(populate_existing=True)
                .options(
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

        # Distinct IDs keep a patch touching several files in one
        # subsystem from counting more than once.
        counts: dict[int, set[int]] = defaultdict(set)
        last_activity: dict[int, datetime] = {}
        for r in rows:
            inc = matches_for(r.path, include_prefix, include_exact)
            if not inc:
                continue
            exc = matches_for(r.path, exclude_prefix, exclude_exact)
            matched = inc - exc
            if not matched:
                continue
            path_date = coerce_dt(r.art_date)
            # SQL excludes NULL dates; persisted DateTime values are ISO strings.
            assert path_date is not None
            for sub_id in matched:
                counts[sub_id].add(r.article_id)
                prev = last_activity.get(sub_id)
                if prev is None or path_date > prev:
                    last_activity[sub_id] = path_date

        # Callers alone choose display limits; directories need every match.
        ranked = sorted(
            counts.items(),
            key=lambda kv: (-len(kv[1]), -last_activity[kv[0]].timestamp()),
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
                )
            )
        if rules_version(session) != version:
            raise _RulesChanged
        return out

    key = f"most_active_subsystems_in_inbox:v2:{inbox.name}:{days}:{version}"
    if not compute_on_miss:
        # Request-path posture: cache hit serves immediately, cache
        # miss serves empty so the page render never blocks on the
        # multi-second aggregation. Warm-cache keeps the row fresh.
        return cache.get(key)
    try:
        return cache.get_or_compute(
            session, key, MOST_ACTIVE_SUBSYSTEMS_CACHE_TTL_SEC, compute, force=force
        )
    except _RulesChanged:
        return None


def most_active_subsystems_global(
    session: Session,
    days: int = 7,
    limit: int | None = 10,
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
    return (full or [])[:limit]


def _most_active_subsystems_global_full(
    session: Session,
    days: int = 7,
    force: bool = False,
    *,
    compute_on_miss: bool = True,
) -> list[SubsystemActivity] | None:
    """Complete cached cross-inbox ranking, or None when cold.
    Counts sum per-inbox membership, preserving the existing cross-post semantics. Each subsystem appears at most once; the row's
    `inbox_name` is the inbox where this subsystem saw the most
    activity in the window, so the chip links the reader to the
    busiest variant. Inbox-name tie-break is alphabetical for
    determinism.

    Implementation: consume each inbox's already-cached full ranked
    list, merge by subsystem id, pick the busiest inbox per subsystem
    and re-sort globally without truncation.
    """

    version = rules_version(session)

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
            rows = _most_active_subsystems_in_inbox_full(
                session, inbox, days=days, force=force
            )
            if rows is None:
                raise _RulesChanged
            for row in rows:
                entry = agg.get(row.id)
                if entry is None:
                    agg[row.id] = {
                        "name": row.name,
                        "total": row.message_count,
                        "best_count": row.message_count,
                        "best_inbox": row.inbox_name,
                        "best_last_activity": row.last_activity,
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
        out = [
            SubsystemActivity(
                id=sub_id,
                name=e["name"],
                inbox_name=e["best_inbox"],
                message_count=e["total"],
                last_activity=e["best_last_activity"],
            )
            for sub_id, e in agg.items()
        ]
        out.sort(key=lambda a: (-a.message_count, -a.last_activity.timestamp()))
        if rules_version(session) != version:
            raise _RulesChanged
        return out

    key = f"most_active_subsystems_global:v2:{days}:{version}"
    if not compute_on_miss:
        return cache.get(key)
    try:
        return cache.get_or_compute(
            session, key, MOST_ACTIVE_SUBSYSTEMS_CACHE_TTL_SEC, compute, force=force
        )
    except _RulesChanged:
        return None
