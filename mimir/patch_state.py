"""Per-patch state aggregation for the message-page state card (#208).

The card consolidates four signals readers want at a glance when
landing on a patch page:

- Trailer roll-up across `article_trailers`, with a maintainer-vs-
  random-reviewer distinction sourced from `subsystem_maintainers`.
- Mainline-landing record from `mainline_commits` reverse lookup.
- Patch-series timeline (v1, v2, v3) with `[diff vs current]` links,
  carried over from #210 so the new card REPLACES the existing
  patch-series sidebar without losing the diff navigation.
- Thread activity (days since last reply), derived from the already-
  loaded thread tree.

The route assembles inputs (article, thread, subsystem hits, the
inbox-link list) and passes them in; this helper queries only what
isn't already available there. Resulting `PatchState` round-trips
through `mimir.cache` so the state card can be served from cache
within the message-page TTL.

Wording note: the "Not in mainline as of <timestamp>" framing from
the issue is deferred. `mainline_state` carries SHA cursors only;
adding a timestamp column for that one render-side wording is out
of scope here. We show the landing row when landed and omit when
not, which reads as "still in flight" without explicit framing.

Nack/NAK trailers are NOT in `trailers.INDEXED_TRAILER_ROLES` today
and so don't surface in the roll-up. Indexing them needs a parser
extension + a backfill; that's its own follow-up.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from mimir import cache
from mimir.datetime_utils import aware_utc
from mimir.models import (
    Article,
    ArticleList,
    ArticleTrailer,
    MainlineCommit,
    SubsystemMaintainer,
)
from mimir.patch_series import parse_cover_letter
from mimir.patch_revisions import parse_in_series_patch_subject
from mimir.trailers import INDEXED_TRAILER_ROLES
from mimir.web.urls import _canonical_url_for


@dataclass
class StateTrailerCount:
    """One row of the trailer roll-up. `maintainer_count` is the
    subset of `total` whose `address_normalized` matches an M:/R:
    address on any subsystem this patch touches; 0 when no
    attestation came from a recognised maintainer."""

    role: str
    total: int
    maintainer_count: int


@dataclass
class StateMainlineLanding:
    """One mainline-commit landing for the patch. Mirrors the
    fields the existing `mainline_applications` aside renders so
    we don't lose information when consolidating."""

    commit_sha: str
    tree_name: str
    tree_label: str  # user-facing display name; falls back to tree_name slug
    committed_at: datetime | None


@dataclass
class StateSeriesEntry:
    """One revision in the series timeline. Carries everything the
    template needs to render either the bold current-revision marker
    or the link + `[diff vs current]` chip, without re-querying."""

    version: str
    article_id: int
    date: datetime | None
    url: str
    is_current: bool
    diff_url: str | None


@dataclass
class StateTimelineEvent:
    """One ordered event in the patch's journey. `kind` is a stable
    key (`posted`, `trailer:Reviewed-by`, `tree:linus`, ...); `label`
    is the human-readable display; `detail` carries optional context
    (e.g. "commit deadbeef...", "Bob (maintainer)"). Sortable by
    `when`."""

    kind: str
    label: str
    when: datetime
    detail: str | None = None


@dataclass
class PatchState:
    """The four-row state card for a patch's message page. `is_patch`
    drives whether the card renders at all (non-patch articles get
    no card). Empty list fields skip their respective rows.

    `activity_heat` and `activity_detail` default to the "dormant"
    values so existing cache rows deserialise cleanly during rollout
    before they are recomputed with the new fields populated."""

    is_patch: bool
    trailers: list[StateTrailerCount]
    mainline_landings: list[StateMainlineLanding]
    series: list[StateSeriesEntry]
    days_since_last_reply: int | None
    timeline: list[StateTimelineEvent]
    activity_heat: str = "dormant"  # heat-class from _activity_heat
    activity_detail: str = "no replies"  # count-label from _activity_heat


cache.register("StateTrailerCount", StateTrailerCount)
cache.register("StateMainlineLanding", StateMainlineLanding)
cache.register("StateSeriesEntry", StateSeriesEntry)
cache.register("StateTimelineEvent", StateTimelineEvent)
cache.register("PatchState", PatchState)


# Same TTL as the threading helpers feeding the activity row; the
# state card's freshness doesn't need to outrun the data it summarises.
PATCH_STATE_CACHE_TTL_SEC = 300


def _is_patch_subject(subject: str | None) -> bool:
    """True when the subject reads as a `[PATCH ...]` shape (cover
    letter, in-series patch, or solo `[PATCH] foo`). False for
    replies, prose, and `[GIT PULL]`/`[ANNOUNCE]` brackets."""
    if not subject:
        return False
    if parse_cover_letter(subject) is not None:
        return True
    if parse_in_series_patch_subject(subject) is not None:
        return True
    # Solo patch: `[PATCH] title` or `[PATCH v2] title`. Neither
    # parser matches (both require N/M); a cheap startswith check
    # plus the PATCH-token guard is enough.
    stripped = subject.strip()
    if not stripped.startswith("["):
        return False
    close = stripped.find("]")
    if close == -1:
        return False
    inside = stripped[1:close]
    return "PATCH" in inside.upper().split()


def _trailer_roll_up(
    session: Session,
    article: Article,
    subsystem_ids: Iterable[int],
) -> list[StateTrailerCount]:
    """Aggregate `article_trailers` into per-role counts, marking
    the subset attested by an M:/R: maintainer of any subsystem
    this patch touches.

    Ordering: same order as `INDEXED_TRAILER_ROLES` so the rendered
    pill row is deterministic and matches the trailer-extraction
    canon. Roles with zero counts are skipped (silent rows defeat
    the at-a-glance value).
    """
    # Load this article's trailers. The `article.trailers`
    # relationship would also work; an explicit SELECT keeps the
    # session footprint predictable for the cache compute path.
    trailer_rows = session.execute(
        select(ArticleTrailer.role, ArticleTrailer.address_normalized).where(
            ArticleTrailer.article_id == article.id
        )
    ).all()
    if not trailer_rows:
        return []

    # Maintainer-address set, lowercased to match
    # `ArticleTrailer.address_normalized` from ingest.
    subsystem_id_list = [sid for sid in subsystem_ids]
    maintainer_addrs: set[str] = set()
    if subsystem_id_list:
        rows = session.execute(
            select(SubsystemMaintainer.address).where(
                SubsystemMaintainer.subsystem_id.in_(subsystem_id_list),
                SubsystemMaintainer.role.in_(("M", "R")),
            )
        ).all()
        maintainer_addrs = {(addr or "").lower().strip() for (addr,) in rows if addr}

    # Counts per (canonical role); maintainer subset per role.
    role_total: dict[str, int] = {}
    role_maintainer: dict[str, int] = {}
    for role, addr_norm in trailer_rows:
        role_total[role] = role_total.get(role, 0) + 1
        if addr_norm and addr_norm in maintainer_addrs:
            role_maintainer[role] = role_maintainer.get(role, 0) + 1

    return [
        StateTrailerCount(
            role=role,
            total=role_total[role],
            maintainer_count=role_maintainer.get(role, 0),
        )
        for role in INDEXED_TRAILER_ROLES
        if role in role_total
    ]


def _resolve_tree_label(tree_name: str) -> str:
    """Look up `Settings.trees[tree_name].display_name`; fall back to
    the slug itself when no display_name is set or the tree isn't in
    config (e.g. mainline_commits row from a tree the operator removed)."""
    from mimir.config import settings

    cfg = settings.trees.get(tree_name)
    if cfg is None or cfg.display_name is None:
        return tree_name
    return cfg.display_name


# Per-tree label mapping for the timeline event display.
_TREE_EVENT_LABEL = {
    "linus": "Landed",
    "linux-next": "Aggregated",
}


def _tree_event_label(tree_name: str) -> str:
    """Tree pickups outside linus/-next display as 'Picked up'."""
    return _TREE_EVENT_LABEL.get(tree_name, "Picked up")


def _lifecycle_timeline(
    session: Session,
    article: Article,
) -> list[StateTimelineEvent]:
    """Build the chronological event list for the patch's journey.
    Order: posted -> first review trailer (per role) -> per-tree
    pickup (chronological). Per-role dedup means multiple
    Reviewed-by trailers collapse to the earliest one.

    ArticleTrailer has no `created_at` column; the article's own
    `date` is used as the trailer-event timestamp. This means all
    trailer events for a given article share the same `when` value,
    which is imprecise but durable against schema evolution. The
    sort key is stable enough for display purposes.
    """
    events: list[StateTimelineEvent] = []

    if article.date is not None:
        events.append(
            StateTimelineEvent(
                kind="posted",
                label="Posted",
                when=aware_utc(article.date),
                detail=article.author,
            )
        )

    # Per-role earliest trailer. ArticleTrailer carries no created_at;
    # fall back to article.date as the trailer-event timestamp.
    trailer_rows = session.execute(
        select(
            ArticleTrailer.role,
            ArticleTrailer.name,
        ).where(ArticleTrailer.article_id == article.id)
    ).all()
    role_earliest: dict[str, tuple[datetime, str | None]] = {}
    for role, name in trailer_rows:
        when = aware_utc(article.date) if article.date else datetime.now(timezone.utc)
        if role not in role_earliest or when < role_earliest[role][0]:
            role_earliest[role] = (when, name)
    for role, (when, name) in role_earliest.items():
        events.append(
            StateTimelineEvent(
                kind=f"trailer:{role}",
                label=role,
                when=when,
                detail=name,
            )
        )

    # Per-tree earliest landing.
    landing_rows = session.execute(
        select(
            MainlineCommit.tree_name,
            MainlineCommit.commit_sha,
            MainlineCommit.committed_at,
        ).where(MainlineCommit.message_id == article.message_id)
    ).all()
    tree_earliest: dict[str, tuple[datetime, str]] = {}
    for tree_name, sha, committed_at in landing_rows:
        when = aware_utc(committed_at)
        if tree_name not in tree_earliest or when < tree_earliest[tree_name][0]:
            tree_earliest[tree_name] = (when, sha)
    for tree_name, (when, sha) in tree_earliest.items():
        events.append(
            StateTimelineEvent(
                kind=f"tree:{tree_name}",
                label=f"{_tree_event_label(tree_name)} on {_resolve_tree_label(tree_name)}",
                when=when,
                detail=f"commit {sha[:12]}",
            )
        )

    events.sort(key=lambda e: e.when)
    return events


def _mainline_landings(
    session: Session,
    article: Article,
) -> list[StateMainlineLanding]:
    """Mainline-tree commits referencing this article via `Link:`
    trailer. Mirrors the existing `mainline_applications` query;
    same ordering."""
    rows = (
        session.execute(
            select(MainlineCommit)
            .where(MainlineCommit.message_id == article.message_id)
            .order_by(MainlineCommit.committed_at.asc())
        )
        .scalars()
        .all()
    )
    return [
        StateMainlineLanding(
            commit_sha=c.commit_sha,
            tree_name=c.tree_name,
            tree_label=_resolve_tree_label(c.tree_name),
            committed_at=c.committed_at,
        )
        for c in rows
    ]


def _series_timeline(
    session: Session,
    article: Article,
    inbox_name: str,
) -> list[StateSeriesEntry]:
    """Build the revision timeline + per-revision `[diff vs current]`
    links for the current article's series position. Returns an
    empty list when the article isn't a series patch (NULL
    `patch_series_key`).

    Same-position filter (`patch_series_position`) is essential
    after #212: in-series patches now also carry `patch_series_key`,
    so a plain `key`-only filter would return every patch in every
    revision. The position filter narrows to "every revision of THIS
    patch" (e.g. all `[PATCH N/M v*] foo: add bar` rows across v1,
    v2, v3). Cover letters get `position = 0` and the same filter
    surfaces their cross-revision timeline as before.

    `diff_url` carries `pos=cover` for the cover row and `pos=N`
    for in-series patches, matching the `series_diff` route's
    request shape (see `mimir/web/routes/series_diff.py`).
    """
    if not article.patch_series_key:
        return []
    revisions = list(
        session.execute(
            select(Article)
            .options(selectinload(Article.lists).selectinload(ArticleList.inbox))
            .where(
                Article.patch_series_key == article.patch_series_key,
                # Same-position filter, see docstring.
                Article.patch_series_position == article.patch_series_position,
            )
            .order_by(Article.date.asc().nulls_last())
        ).scalars()
    )
    if len(revisions) < 2:
        return []
    current_version = article.patch_series_version
    # `pos=cover` vs `pos=N` matches the `series_diff` route's
    # accepted shape; covers have position 0 and the route accepts
    # both `cover` and `0`, we use the friendlier alias. Defensive
    # `None` mapping to "cover": pre-#212 rows had key set on covers
    # only with NULL position; the migration backfills those to 0,
    # but a partially-rolled-out deploy could observe the NULL state
    # briefly.
    pos = article.patch_series_position
    pos_param = "cover" if pos in (None, 0) else str(pos)
    out: list[StateSeriesEntry] = []
    for rev in revisions:
        link_set = [(al.inbox_id, al.inbox.name) for al in rev.lists]
        url = _canonical_url_for(rev, link_set, base="") or ""
        is_current = rev.id == article.id
        diff_url: str | None = None
        if not is_current and rev.patch_series_version and current_version:
            diff_url = (
                f"/{inbox_name}/series/{article.patch_series_key}"
                f"/diff?from={rev.patch_series_version}"
                f"&to={current_version}&pos={pos_param}"
            )
        out.append(
            StateSeriesEntry(
                version=rev.patch_series_version or "?",
                article_id=rev.id,
                date=rev.date,
                url=url,
                is_current=is_current,
                diff_url=diff_url,
            )
        )
    return out


def _days_since_last_reply(
    article: Article,
    thread_dates: Iterable[datetime | None],
) -> int | None:
    """Days between now (UTC) and the most recent thread message that
    isn't the article itself. None when the article has no replies
    yet. Capped at >=0 to keep the template safe against future-
    dated outliers (same defensive clamp as the active-threads
    score in `threading._active_threads_query`)."""
    article_date = aware_utc(article.date) if article.date else None
    later_dates = [
        aware_utc(d)
        for d in thread_dates
        if d is not None and (article_date is None or aware_utc(d) > article_date)
    ]
    if not later_dates:
        return None
    last_reply = max(later_dates)
    delta = datetime.now(timezone.utc) - last_reply
    return max(int(delta.days), 0)


def _activity_heat(days_since_last_reply: int | None) -> tuple[str, str]:
    """Map days-since-last-reply to (heat-class, count-label). Six
    buckets calibrated to kernel patch-review cadence:
    same-day = hot, within work-week = warm, within two work-weeks
    = cooling, then degrading on month-scale.

    Returns ("dormant", "no replies") when None (no replies yet),
    distinct from "stale" (replies have stopped). The bucket
    boundaries are inclusive on the upper end."""
    if days_since_last_reply is None:
        return ("dormant", "no replies")
    d = days_since_last_reply
    if d == 0:
        return ("hot", "today")
    if d <= 3:
        return ("warm", f"{d}d")
    if d <= 14:
        return ("cooling", f"{d}d")
    if d <= 60:
        return ("cold", f"{d}d")
    return ("stale", f"{d}d")


def patch_state_for_article(
    session: Session,
    article: Article,
    *,
    thread_dates: Iterable[datetime | None],
    subsystem_ids: Iterable[int],
    inbox_name: str,
    force: bool = False,
) -> PatchState:
    """Compute the state card for `article` on `inbox_name`. The
    route is expected to have already loaded the article's thread
    (for `thread_dates`) and subsystem hits (for `subsystem_ids`)
    so we don't re-query those here.

    Returns `PatchState(is_patch=False, ...)` (with empty rows) for
    non-patch articles; the template skips the whole card when
    `is_patch` is False.
    """
    is_patch = _is_patch_subject(article.subject)
    if not is_patch:
        return PatchState(
            is_patch=False,
            trailers=[],
            mainline_landings=[],
            series=[],
            days_since_last_reply=None,
            timeline=[],
            activity_heat="dormant",
            activity_detail="no replies",
        )

    # Capture thread dates outside the cache closure. The iterable
    # would otherwise be exhausted before the compute function ran
    # on a cache miss, which closes over `thread_dates` lazily.
    thread_dates_list = list(thread_dates)
    subsystem_ids_list = list(subsystem_ids)

    def compute() -> PatchState:
        days = _days_since_last_reply(article, thread_dates_list)
        heat, detail = _activity_heat(days)
        return PatchState(
            is_patch=True,
            trailers=_trailer_roll_up(session, article, subsystem_ids_list),
            mainline_landings=_mainline_landings(session, article),
            series=_series_timeline(session, article, inbox_name),
            days_since_last_reply=days,
            timeline=_lifecycle_timeline(session, article),
            activity_heat=heat,
            activity_detail=detail,
        )

    # Cache key: per-article. Inbox name isn't keyed in because for
    # any single article the per-inbox URL bits (`url`, `diff_url`)
    # are stable across renders, the series-timeline URLs are built
    # from `_canonical_url_for` which picks the same inbox
    # regardless of the requesting URL's inbox. Two inboxes
    # rendering the same cross-posted article see the same card.
    return cache.get_or_compute(
        session,
        f"patch_state:{article.id}",
        PATCH_STATE_CACHE_TTL_SEC,
        compute,
        force=force,
    )


__all__ = [
    "PATCH_STATE_CACHE_TTL_SEC",
    "PatchState",
    "StateMainlineLanding",
    "StateSeriesEntry",
    "StateTimelineEvent",
    "StateTrailerCount",
    "patch_state_for_article",
]
