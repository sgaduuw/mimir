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

from mimir import cache
from mimir.config import settings
from mimir.extensions import SessionLocal, engine, write_transaction
from mimir.ingest.epoch import (
    DEFAULT_WORKERS,
    IngestResult,
    _maybe_promote_list_address,
    ingest_epoch,
)
from mimir.models import Inbox

logger = logging.getLogger(__name__)


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
    with write_transaction(), SessionLocal() as session:
        # Re-attach the Inbox to this session so .id reads work after
        # the caller's session was closed.
        attached = session.merge(inbox)
        # Capture the empty-vs-populated state *from the DB* (not via
        # the merged ORM attribute, which would inherit a possibly-
        # stale value from the input object) before any ingest_epoch
        # call mutates last_article_date in this session. Used below
        # to decide whether this run is an empty-to-non-empty
        # transition that should bust the per-inbox cache.
        was_empty = session.execute(
            select(Inbox.last_article_date).where(Inbox.id == attached.id)
        ).scalar_one() is None
        for epoch_path in discover_epochs(Path(attached.mirror_path)):
            if remaining is not None and remaining <= 0:
                break
            r = ingest_epoch(
                session, attached, epoch_path.name, epoch_path,
                limit=remaining, workers=workers,
            )
            results.append(r)
            if remaining is not None:
                remaining -= r.new + r.linked + r.dup_batch + r.dup_db + r.failed

    # Promote `Inbox.list_address` if we now have enough observations.
    # Cheap: at most two rows queried, one update if it fires.
    with write_transaction(), SessionLocal() as session:
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

    return results


def ingest_all(
    inboxes: dict[str, Inbox] | None = None,
    limit: int | None = None,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, list[IngestResult]]:
    """Ingest every supplied inbox. Returns {inbox_name: [IngestResult, ...]}."""
    if inboxes is None:
        from mimir.inboxes import bootstrap_inboxes
        inboxes = bootstrap_inboxes()

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
