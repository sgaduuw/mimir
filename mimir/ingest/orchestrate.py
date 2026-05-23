"""Per-inbox and across-all-inboxes ingest orchestration.

`discover_epochs` enumerates a mirror's epoch directories. `ingest_inbox`
walks them one at a time and runs `ANALYZE` once enough rows have moved
to invalidate the SQLite query planner's `sqlite_stat1`. `ingest_all`
fans out across every configured inbox.
"""

import logging
from pathlib import Path

from dulwich.errors import NotGitRepository
from dulwich.repo import Repo
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from mimir import cache
from mimir.config import settings
from mimir.dashboard import archive_stats, daily_volume
from mimir.extensions import SessionLocal, engine, write_transaction
from mimir.ingest.epoch import (
    DEFAULT_WORKERS,
    IngestResult,
    _maybe_promote_list_address,
    ingest_epoch,
)
from mimir.models import Inbox
from mimir.subsystems_dashboard import most_active_subsystems_in_inbox

logger = logging.getLogger(__name__)


def _warm_after_ingest(session: Session, inbox: Inbox) -> None:
    """Recover any missing front-page-critical per-inbox cache rows.

    Lazy: each helper is called with `force=False`, so a present cache
    row is returned untouched (one SELECT, ~ms) and only a missing
    or expired row triggers the compute + `cache.set`. The intent is to
    plug warm-cache lag, not to displace it: when warm-cache reliably
    refreshes a row every minute, this helper is a no-op cost; when
    warm-cache has fallen behind (or never reached a freshly-added
    inbox), the next ingest tick proactively rebuilds the row so the
    next request to `/` doesn't pay the cold-miss recompute inline.
    Importantly, this keeps the 24h `archive_stats` TTL property
    intact: a steady-state UPDATE_EVERY=300s tick doesn't re-run the
    multi-second COUNT(*) on every fire.

    Runs *outside* `ingest_inbox`'s `write_transaction()` block on
    purpose. `cache.set` opens its own write session (the cache module
    is session-independent by design); if the warm ran under
    `write_transaction()`, that helper's ContextVar would leak into
    `cache.set`'s session, upgrade its `BEGIN` to `BEGIN IMMEDIATE`,
    and self-deadlock against the writer lock the outer block already
    holds. The read SELECT in this helper uses the normal deferred
    BEGIN and doesn't contend with anything.

    Scope is deliberately narrow: just the helpers that drive the meta-
    index `/` cards (`archive_stats`, `daily_volume`) and the per-inbox
    dashboard's subsystem-discoverability widget. Active-threads /
    today / yesterday / pulls / stable / trackers etc. keep flowing
    through `warm-cache`; their compute is cheap enough that a
    per-ingest-tick top-up would be wasteful. Cross-inbox aggregates
    (`most_active_subsystems_global`) also stay with `warm-cache`,
    they fan in from every inbox and re-running per-inbox-ingest is
    the wrong granularity.

    Best-effort: caller wraps in `try/except` so a failed warm doesn't
    crash the ingest tick.
    """
    archive_stats(session, inbox)
    daily_volume(session, inbox, days=30)
    most_active_subsystems_in_inbox(session, inbox, days=7)


def discover_epochs(mirror_path: Path) -> list[Path]:
    epochs = []
    for child in sorted(mirror_path.iterdir()):
        if not child.is_dir():
            continue
        try:
            Repo(str(child))
        except NotGitRepository:
            continue
        epochs.append(child)
    return epochs


def ingest_inbox(
    inbox: Inbox,
    limit: int | None = None,
    workers: int = DEFAULT_WORKERS,
) -> list[IngestResult]:
    """Ingest every epoch under one inbox's mirror path."""
    results: list[IngestResult] = []
    remaining = limit
    # BEGIN IMMEDIATE on every transaction in this block. ingest_epoch
    # interleaves SELECTs (dedup checks, list-address map refresh) with
    # INSERTs (new articles, article_lists rows, observations), so a
    # gunicorn cache.set committing between two of those statements
    # would trip SQLITE_BUSY_SNAPSHOT and roll back the in-progress
    # batch.
    with (
        write_transaction(f"ingest_inbox:{inbox.name}"),
        SessionLocal() as session,
    ):
        # Re-attach the Inbox to this session so .id reads work after
        # the caller's session was closed.
        attached = session.merge(inbox)
        # Capture the empty-vs-populated state *from the DB* (not via
        # the merged ORM attribute, which would inherit a possibly-
        # stale value from the input object) before any ingest_epoch
        # call mutates last_article_date in this session. Used below
        # to decide whether this run is an empty-to-non-empty
        # transition that should bust the per-inbox cache.
        was_empty = (
            session.execute(
                select(Inbox.last_article_date).where(Inbox.id == attached.id)
            ).scalar_one()
            is None
        )
        for epoch_path in discover_epochs(Path(attached.mirror_path)):
            if remaining is not None and remaining <= 0:
                break
            r = ingest_epoch(
                session,
                attached,
                epoch_path.name,
                epoch_path,
                limit=remaining,
                workers=workers,
            )
            results.append(r)
            if remaining is not None:
                remaining -= r.new + r.linked + r.dup_batch + r.dup_db + r.failed

    # Promote `Inbox.list_address` if we now have enough observations.
    # Cheap: at most two rows queried, one update if it fires.
    with (
        write_transaction(f"promote_list_address:{inbox.name}"),
        SessionLocal() as session,
    ):
        _maybe_promote_list_address(session, inbox.id)
        session.commit()

    # Refresh planner stats when we've moved enough rows that prior
    # `sqlite_stat1` can no longer be trusted, most importantly the
    # first ingest of a freshly-added inbox, which lands a whole archive
    # in one go and would otherwise leave the planner blind until the
    # next scheduled ANALYZE.
    threshold = settings.analyze_after_ingest_rows
    moved = sum(r.new + r.linked for r in results)
    if threshold > 0 and moved >= threshold:
        logger.info("auto-ANALYZE after %s/%d rows ingested", inbox.name, moved)
        # Wrap in write_transaction so the writer-lock-hold duration
        # surfaces in the slow-write WARNING with a clear label. The
        # ANALYZE itself can run tens of seconds on a multi-million-row
        # corpus; without the label an operator sees a slow broker
        # dispatch and has to triangulate to figure out it was the
        # post-ingest ANALYZE rather than the ingest commits.
        with write_transaction(f"auto_analyze:{inbox.name}"):
            with engine.begin() as conn:
                conn.execute(text("ANALYZE"))

    # First-ingest cache bust. `archive_stats:<inbox>` is cached for
    # 24h, so when a freshly-added inbox happened to be warmed in
    # the seconds between `admin inbox add` and the first ingest,
    # the front-page card sticks on `total=0` ("not yet ingested")
    # until the TTL aged out. Drop the per-inbox keys exactly on
    # the empty-to-non-empty transition: targeted enough that
    # steady-state ingests of established inboxes don't trigger
    # the COUNT(*) refresh on every tick.
    if was_empty and moved > 0:
        cache.delete_for_inbox(inbox.name)

    # Eager per-inbox cache warm whenever this tick moved rows. Closes
    # the freshness gap between an ingest commit and the next
    # `warm-cache` tick (60s by default). Scope is the front-page-
    # critical per-inbox helpers only, see `_warm_after_ingest` for
    # which and why. No `write_transaction()` wrap here on purpose:
    # `cache.set` opens its own session, and the outer
    # `write_transaction()`'s ContextVar would otherwise leak into it
    # and self-deadlock on the writer lock. Best-effort; a failed
    # warm doesn't fail the ingest tick.
    if moved > 0:
        try:
            with SessionLocal() as warm_session:
                attached = warm_session.merge(inbox)
                _warm_after_ingest(warm_session, attached)
        except Exception:
            logger.warning(
                "post-ingest cache warm failed for %s",
                inbox.name,
                exc_info=True,
            )

    return results


def ingest_all(
    inboxes: dict[str, Inbox] | None = None,
    limit: int | None = None,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, list[IngestResult]]:
    """Ingest every supplied inbox. Returns {inbox_name: [IngestResult, ...]}."""
    if inboxes is None:
        # Read-only: the broker self-bootstraps `inboxes` on its
        # startup and post-2.0.0 every other process opens
        # `PRAGMA query_only=1`, so calling `bootstrap_inboxes()`
        # here would raise on the readonly connection and abort
        # the entire ingest tick (the LKML-stoppage incident on
        # 2026-05-23). The CLI layer hands us the dict via
        # `_select_inboxes`; this fallback is defensive for
        # library-mode callers.
        from mimir.inboxes import list_inboxes

        inboxes = {inbox.name: inbox for inbox in list_inboxes()}

    out: dict[str, list[IngestResult]] = {}
    remaining = limit
    for name, inbox in inboxes.items():
        if remaining is not None and remaining <= 0:
            break
        rs = ingest_inbox(inbox, limit=remaining, workers=workers)
        out[name] = rs
        if remaining is not None:
            for r in rs:
                remaining -= r.new + r.linked + r.dup_batch + r.dup_db + r.failed
    return out
