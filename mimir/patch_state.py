"""Per-patch state aggregation for the message-page state card (#208).

The card consolidates three signals readers want at a glance when
landing on a patch page:

- Trailer roll-up across `article_trailers`, with a maintainer-vs-
  random-reviewer distinction sourced from `subsystem_maintainers`.
- Patch-series timeline (v1, v2, v3) with diff-against-previous links,
  carried over from #210 so the new card REPLACES the existing
  patch-series sidebar without losing the diff navigation.
- Thread activity (days since last reply), derived from the already-
  loaded thread tree.

The route assembles inputs (article, thread, subsystem hits, the
inbox-link list) and passes them in; this helper queries only what
isn't already available there. Resulting `PatchState` round-trips
through `mimir.cache` so the state card can be served from cache
within the message-page TTL.

The landing record is not here: the status line reads it from the
lifecycle row (`LifecycleStatusInfo`), the same row the pill reads, so
the two cannot name different commits (#844).

Nack/NAK trailers are NOT in `trailers.INDEXED_TRAILER_ROLES` today
and so don't surface in the roll-up. Indexing them needs a parser
extension + a backfill; that's its own follow-up.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from mimir import cache
from mimir.datetime_utils import aware_utc
from mimir.models import (
    Article,
    ArticleList,
    ArticleTrailer,
    SubsystemMaintainer,
)
from mimir.patch_revisions import parse_in_series_patch_subject, patch_bracket
from mimir.patch_series import adjacent_versions, parse_cover_letter, same_series
from mimir.trailers import INDEXED_TRAILER_ROLES
from mimir.web.urls import _canonical_url_for, _series_diff_url


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
class StateSeriesEntry:
    """One revision in the series timeline. Carries everything the
    template needs to render either the bold current-revision marker
    or the link, plus the diff-against-previous link, without
    re-querying. `diff_url` diffs the previous revision against this
    one, and is None for the first revision and for a resend."""

    version: str
    article_id: int
    date: datetime | None
    url: str
    is_current: bool
    diff_url: str | None


@dataclass
class PatchState:
    """The state card for a patch's message page. `is_patch` drives
    whether the card renders at all (non-patch articles get no
    card). Empty list fields skip their respective rows."""

    is_patch: bool
    trailers: list[StateTrailerCount]
    series: list[StateSeriesEntry]
    days_since_last_reply: int | None


cache.register("StateTrailerCount", StateTrailerCount)
cache.register("StateSeriesEntry", StateSeriesEntry)
cache.register("PatchState", PatchState)


# Same TTL as the threading helpers feeding the activity row; the
# state card's freshness doesn't need to outrun the data it summarises.
PATCH_STATE_CACHE_TTL_SEC = 300


def _is_patch_subject(subject: str | None) -> bool:
    """True when the subject is a patch: a cover letter, an in-series
    patch or a solo `[PATCH] foo`, list-tagged or not. `patch_bracket`
    decides, the same predicate both series parsers use (#711), so the
    state card cannot disagree with them. False for replies, prose,
    `[GIT PULL]`, `[ANNOUNCE]` and `[RFC]` without a numbered bracket."""
    return bool(subject) and patch_bracket(subject) is not None


def _is_reply_to_patch(article: Article) -> bool:
    """True when a cover or in-series subject has neither a stored
    position nor a stored key. Ingest and the reprocess clear both on a
    reply that repeats such a subject (#820, #837); a real patch keeps
    its position and a cover stored before the #212 backfill keeps its
    key. A solo `[PATCH]` never has either, so it is not judged here
    (#838)."""
    return (
        article.patch_series_position is None
        and article.patch_series_key is None
        and (
            parse_cover_letter(article.subject) is not None
            or parse_in_series_patch_subject(article.subject) is not None
        )
    )


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


def _series_timeline(
    session: Session,
    article: Article,
    inbox_name: str,
) -> list[StateSeriesEntry]:
    """Build the revision timeline + per-revision diff-against-previous
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
                same_series(article.patch_series_key, article.patch_series_head_id),
                # Same-position filter, see docstring. NULL is 0, as in
                # `patch_series.newer_revision_sql`, so the fold lists
                # what the pill counts.
                func.coalesce(Article.patch_series_position, 0)
                == (article.patch_series_position or 0),
            )
            # The id breaks ties as `adjacent_versions` does, so a
            # version's first row here is the one its link sits on.
            .order_by(Article.date.asc().nulls_last(), Article.id)
        ).scalars()
    )
    if len(revisions) < 2:
        return []
    # Each step links on the first row of its newer version. A resend
    # repeats a version, and the step is already on its first copy.
    previous = {
        newer: older
        for older, newer in adjacent_versions(
            (r.date, r.id, r.patch_series_version) for r in revisions
        )
    }
    # `pos=cover` vs `pos=N` matches the `series_diff` route's
    # accepted shape; covers have position 0 and the route accepts
    # both `cover` and `0`, we use the friendlier alias. Defensive
    # `None` mapping to "cover": pre-#212 rows had key set on covers
    # only with NULL position; the migration backfills those to 0,
    # but a partially-rolled-out deploy could observe the NULL state
    # briefly.
    pos = article.patch_series_position
    out: list[StateSeriesEntry] = []
    for rev in revisions:
        link_set = [(al.inbox_id, al.inbox.name) for al in rev.lists]
        url = _canonical_url_for(rev, link_set, base="") or ""
        is_current = rev.id == article.id
        diff_url: str | None = None
        older = previous.pop(rev.patch_series_version, None)
        if older is not None:
            to_slot = [
                r
                for r in revisions
                if r.patch_series_version == rev.patch_series_version
            ]
            diff_url = _series_diff_url(
                to_slot,
                article.patch_series_key,
                older,
                rev.patch_series_version,
                pos,
                head_id=article.patch_series_head_id,
                fallback_inbox=inbox_name,
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
    delta = datetime.now(UTC) - last_reply
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
    state_key: str,
    force: bool = False,
) -> PatchState:
    """Compute the state card for `article` on `inbox_name`. The
    route is expected to have already loaded the article's thread
    (for `thread_dates`) and subsystem hits (for `subsystem_ids`)
    so we don't re-query those here.

    `state_key` is the digest of the page ETag's content part (the ETag
    without its page and full/htmx fields), and keys the cache, so a
    cached card is reused only while that content is unchanged.
    Required: both callers serve this card under an ETag, and a card
    cached by article id alone outlived the validator and pinned
    "Queued in linux-next" under the ETag of a landed patch.

    Returns `PatchState(is_patch=False, ...)` (with empty rows) for
    non-patch articles; the template skips the whole card when
    `is_patch` is False.
    """
    is_patch = _is_patch_subject(article.subject) and not _is_reply_to_patch(article)
    if not is_patch:
        return PatchState(
            is_patch=False,
            trailers=[],
            series=[],
            days_since_last_reply=None,
        )

    # Capture thread dates outside the cache closure. The iterable
    # would otherwise be exhausted before the compute function ran
    # on a cache miss, which closes over `thread_dates` lazily.
    thread_dates_list = list(thread_dates)
    subsystem_ids_list = list(subsystem_ids)

    def compute() -> PatchState:
        return PatchState(
            is_patch=True,
            trailers=_trailer_roll_up(session, article, subsystem_ids_list),
            series=_series_timeline(session, article, inbox_name),
            days_since_last_reply=_days_since_last_reply(article, thread_dates_list),
        )

    # Cache key: per-article. Inbox name isn't keyed in because for
    # any single article the per-inbox URL bits (`url`, `diff_url`)
    # are stable across renders, the series-timeline URLs are built
    # from `_canonical_url_for` which picks the same inbox
    # regardless of the requesting URL's inbox. Two inboxes
    # rendering the same cross-posted article see the same card.
    return cache.get_or_compute(
        session,
        f"patch_state:{article.id}:{state_key}",
        PATCH_STATE_CACHE_TTL_SEC,
        compute,
        force=force,
    )


__all__ = [
    "PATCH_STATE_CACHE_TTL_SEC",
    "PatchState",
    "StateSeriesEntry",
    "StateTrailerCount",
    "patch_state_for_article",
]
