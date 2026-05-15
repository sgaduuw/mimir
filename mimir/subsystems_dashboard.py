"""Per-subsystem dashboard helpers + cross-inbox aggregations.

Pulled out of `mimir.subsystems` to keep that module focused on
the article-level path-matching primitives (which inbox lookups
need: `subsystems_for_article`, `recent_patches_touching`,
`path_matches_glob`). This module owns the heavier read-side
fan-out: the four per-subsystem dashboard helpers
(`recent_articles_in_subsystem`, `daily_volume_in_subsystem`,
`active_threads_in_subsystem`, `active_reviewers_in_subsystem`),
the reviewer-page list (`articles_reviewed_by`), and the cross-
inbox "most active subsystems" surfaces that the front-page
teaser and the per-inbox dashboards both consume.

`RelatedPatch` and `path_matches_glob` live in `mimir.subsystems`
(small, no-fan-out neighbours of the message-page header helper);
we import them here. Asymmetric: `subsystems_dashboard` depends
on `subsystems`, not the other way around.

Cache-key conventions are documented inline per helper; bumping
`cache.NAMESPACE_VERSION` invalidates everything if a payload
shape changes.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_cls, datetime, timedelta, timezone

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, selectinload

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
from mimir.threading import (
    ACTIVE_THREADS_CACHE_TTL_SEC,
    ActiveThread,
    _active_threads_query,
    _coerce_dt,
)


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
    skipped silently — they're a small minority of rules and add
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
        # (...)` clause instead of one OR-equality per rule — wide
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
                # as literal — escape them so a path glob containing `_`
                # (rare but possible: `arch/x86_64/`) doesn't widen the
                # match.
                prefix = like_escape(g)
                or_conds.append(ArticleFile.path.like(prefix + "%", escape="\\"))
                # Also match the bare directory path (no trailing slash).
                exact_paths.append(g[:-1])
            elif not any(c in g for c in "*?["):
                exact_paths.append(g)
            # else: wildcard — skipped in slice 1
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
    wildcard) include rules — caller should treat that as "no
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
    dashboard TTL — the recursive CTE is too heavy for the 5min
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
        # seed row — order-of-seconds on wide subsystems like
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


@dataclass
class ReviewerStat:
    """One reviewer's activity in a subsystem over the recent window.

    `name` is the display name from the most-recent trailer the
    reviewer authored (the address is the stable identity; the name
    can drift across messages, and "their latest" is least-surprising
    for the visible label).

    `address` is the verbatim address from the most-recent trailer —
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

    Dataclass (not pydantic) for cache-encoder compatibility — see
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
    Tested-by) shows as two entries — that's accurate to the source
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
    the threads helper. Cache key uses the address verbatim — it's
    already lowercased so casing collisions are impossible.
    """
    def compute() -> list[ReviewEntry]:
        rows = session.execute(
            text(
                """
                SELECT a.id AS article_id, a.message_id, a.subject,
                       a.date AS art_date, t.role,
                       COALESCE(canon.name, fb.name) AS inbox_name
                FROM article_trailers t
                JOIN articles a ON a.id = t.article_id
                JOIN article_lists al ON al.article_id = a.id
                LEFT JOIN inboxes canon
                    ON canon.id = a.canonical_inbox_id
                LEFT JOIN (
                    SELECT al2.article_id, MIN(i.name) AS name
                    FROM article_lists al2
                    JOIN inboxes i ON i.id = al2.inbox_id
                    GROUP BY al2.article_id
                ) fb ON fb.article_id = a.id
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
    review cadence is slower than discussion cadence — a maintainer
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


# Cache TTL for the "most active subsystems" surfaces. Same window
# as `active_threads` since the visual semantics match: "what's hot
# in the last N days". 5 min keeps the page responsive to fresh
# ingest without recomputing the per-subsystem fan-out on every
# request.
MOST_ACTIVE_SUBSYSTEMS_CACHE_TTL_SEC = 300


# How many ranked subsystems we cache per (inbox, days) tuple. Callers
# pass any `limit` ≤ this and slice from the cached list. 100 covers
# every current surface (front-page top-12, inbox dashboard top-10,
# the global aggregator's `limit*3` hedge) without recomputing per
# caller — which was the v1.19.2 warm-cache hot spot: each inbox was
# computing the same expensive aggregation three times for three
# distinct limit suffixes (10, 30, 36).
MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP = 100


def most_active_subsystems_in_inbox(
    session: Session, inbox: Inbox,
    days: int = 7, limit: int = 10, force: bool = False,
) -> list[SubsystemActivity]:
    """Top-N subsystems by message volume in `inbox` over the
    recent window. Powers the "Most active subsystems" widget on
    the per-inbox dashboard.

    Thin slicer over `_most_active_subsystems_in_inbox_full`: the
    cached list is internally capped at
    `MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP`; every caller slices from
    that one cached payload regardless of `limit`. The cache key is
    therefore `(inbox.name, days)` only — adding `limit` to the key
    was the v1.19.2 cold-path waste (three caches for one
    aggregation).
    """
    return _most_active_subsystems_in_inbox_full(
        session, inbox, days=days, force=force,
    )[:limit]


def _most_active_subsystems_in_inbox_full(
    session: Session, inbox: Inbox,
    days: int = 7, force: bool = False,
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
        # in an off-by-one bucket.
        today = date_cls.today()
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
        subs = session.execute(
            select(Subsystem).options(
                selectinload(Subsystem.paths),
                selectinload(Subsystem.maintainers),
            )
        ).scalars().all()
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
                    bucket = (
                        exclude_prefix if rule.is_exclude else include_prefix
                    )
                    bucket[glob[:-1]].add(sub.id)
                elif not any(c in glob for c in "*?["):
                    bucket = (
                        exclude_exact if rule.is_exclude else include_exact
                    )
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
            out.append(SubsystemActivity(
                id=sub_id, name=sub.name,
                inbox_name=inbox.name,
                message_count=len(article_set),
                last_activity=last_activity[sub_id],
                maintainer_name=top_maintainer,
                multiple_maintainers=len(maintainers) > 1,
                status=sub.status,
                spark=_spark_for(sub_id),
            ))
        return out

    return cache.get_or_compute(
        session,
        f"most_active_subsystems_in_inbox:{inbox.name}:{days}",
        MOST_ACTIVE_SUBSYSTEMS_CACHE_TTL_SEC,
        compute,
        force=force,
    )


def most_active_subsystems_global(
    session: Session,
    days: int = 7, limit: int = 10, force: bool = False,
) -> list[SubsystemActivity]:
    """Top-N subsystems across **all** configured inboxes over the
    recent window. Powers the "Active subsystems" teaser on the
    front page (`/`).

    Thin slicer over `_most_active_subsystems_global_full`: same
    limit-less caching shape as the per-inbox helper.
    """
    return _most_active_subsystems_global_full(
        session, days=days, force=force,
    )[:limit]


def _most_active_subsystems_global_full(
    session: Session, days: int = 7, force: bool = False,
) -> list[SubsystemActivity]:
    """Cached full cross-inbox ranking (top `MOST_ACTIVE_SUBSYSTEMS_INTERNAL_CAP`)
    for `days`. Each subsystem appears at most once; the row's
    `inbox_name` is the inbox where this subsystem saw the most
    activity in the window, so the chip links the reader to the
    busiest variant. Inbox-name tie-break is alphabetical for
    determinism.

    Implementation: consume each inbox's already-cached full ranked
    list (no `limit*3` hedge needed — we have the whole top-100 per
    inbox), merge by subsystem id, pick the busiest inbox per
    subsystem, re-sort globally, truncate to the internal cap.
    """
    def compute() -> list[SubsystemActivity]:
        inboxes = session.execute(select(Inbox)).scalars().all()
        agg: dict[int, dict] = {}
        for inbox in inboxes:
            for row in _most_active_subsystems_in_inbox_full(
                session, inbox, days=days,
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

    return cache.get_or_compute(
        session,
        f"most_active_subsystems_global:{days}",
        MOST_ACTIVE_SUBSYSTEMS_CACHE_TTL_SEC,
        compute,
        force=force,
    )

