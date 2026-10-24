"""Per-inbox and across-all-inboxes ingest orchestration.

`discover_epochs` enumerates a mirror's epoch directories. `ingest_inbox`
detects a new inbox's list address from a sample of its newest mail,
walks the epochs one at a time and runs `ANALYZE` once enough rows have moved
to invalidate the SQLite query planner's `sqlite_stat1`. `ingest_all`
fans out across every configured inbox.
"""

import logging
from collections import Counter
from pathlib import Path

from dulwich.errors import NotGitRepository
from dulwich.repo import Repo
from sqlalchemy import select
from sqlalchemy.orm import Session

from mimir import cache
from mimir.broker._context import get_active_pool, get_active_writer
from mimir.canonical import extract_list_addresses
from mimir.config import settings
from mimir.dashboard import archive_stats, daily_volume
from mimir.extensions import SessionLocal
from mimir.ingest._pending import _submit_analyze, _submit_promote_list_address
from mimir.ingest.epoch import (
    DEFAULT_WORKERS,
    IngestResult,
    ingest_epoch,
)
from mimir.models import Inbox
from mimir.parser import parse_message
from mimir.subsystems_dashboard import most_active_subsystems_in_inbox

logger = logging.getLogger(__name__)

# How many of a new inbox's newest messages the pre-walk sample reads
# (#855). Promotion needs MIN_PROMOTE_OBSERVATIONS (50) list-shaped
# addresses, and not every message carries one, so 500 leaves room.
# Unmeasured: on a fresh archive of 203 inboxes it is about 100k parses
# in total, expected to be small beside the walk itself.
LIST_ADDRESS_SAMPLE = 500


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

    Runs on its own `SessionLocal()` session, not on `ingest_inbox`'s
    `pool.session()` (which is `query_only=1` and would reject the
    cache writes the helpers fire). `cache.set` opens its own write
    session internally, so the warm helpers just need a readable
    session to compute against.

    Pre-Phase-3b this docstring documented why the warm ran *outside*
    `ingest_inbox`'s `write_transaction()` block (to avoid leaking
    the BEGIN IMMEDIATE ContextVar into `cache.set`'s session and
    self-deadlocking). Phase 3b removed that block; the helper's
    isolation rule is now simpler (private write session), but the
    "best-effort, never crash the ingest tick" posture is unchanged.

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


def _epoch_number(path: Path) -> tuple[bool, int, str]:
    """Sort key: epochs by number, since by name 10.git sorts before
    2.git. sync clones only numbered epochs, so any other name is a stray
    and goes last, by name."""
    n = path.name.removesuffix(".git")
    return (not n.isdigit(), int(n) if n.isdigit() else 0, path.name)


def discover_epochs(mirror_path: Path) -> list[Path]:
    epochs = []
    for child in sorted(mirror_path.iterdir(), key=_epoch_number):
        if not child.is_dir():
            continue
        try:
            Repo(str(child))
        except NotGitRepository:
            continue
        epochs.append(child)
    return epochs


def _sample_list_addresses(mirror_path: Path) -> dict[str, int]:
    """Count list-shaped To/Cc addresses over the newest
    LIST_ADDRESS_SAMPLE messages of a mirror, newest epoch first, for
    `_submit_promote_list_address` to judge before the first walk. Reads
    git only. A message that fails to parse is skipped here; the walk
    records it as a parse failure."""
    tally: Counter[str] = Counter()
    remaining = LIST_ADDRESS_SAMPLE
    for epoch_path in reversed(discover_epochs(mirror_path)):
        with Repo(str(epoch_path)) as repo:
            try:
                head = repo.head()
            except KeyError:  # no commits yet, as a clone killed by sync leaves
                continue
            walker = repo.get_walker(include=[head], max_entries=remaining)
            for entry in walker:
                remaining -= 1
                try:
                    _mode, blob_sha = repo[entry.commit.tree][b"m"]
                    parsed = parse_message(repo[blob_sha].data)
                except Exception:
                    continue
                tally.update(extract_list_addresses(parsed.headers))
        if remaining <= 0:
            break
    return dict(tally)


def ingest_inbox(
    inbox: Inbox,
    limit: int | None = None,
    workers: int = DEFAULT_WORKERS,
) -> list[IngestResult]:
    """Ingest every epoch under one inbox's mirror path.

    Phase 3b: all writes go through the active WriterThread via WriteOps
    (obtained from _context.get_active_writer()). Reads use a
    query_only session from _context.get_active_pool(). No
    write_transaction() block is held for the epoch walk duration.
    """
    pool = get_active_pool()
    writer = get_active_writer()

    results: list[IngestResult] = []
    remaining = limit

    with pool.session() as session:
        # Capture the empty-vs-populated state *from the DB* before any
        # ingest_epoch call's batch commits mutate last_article_date. Used
        # below to decide whether this run is an empty-to-non-empty
        # transition that should bust the per-inbox cache.
        #
        # We do NOT merge `inbox` into this session. Merging an ORM object
        # into a query_only=1 session marks the object dirty (when the
        # in-memory state differs from the DB, e.g. because a test updated
        # the DB directly after fetching the object), and SQLAlchemy's
        # autoflush fires on the next query attempt, raising
        # OperationalError("attempt to write a readonly database").
        #
        # `inbox.id` is safe on a detached object (assigned at INSERT time
        # and never cleared); `inbox.mirror_path` and `inbox.name` are
        # plain column values that survive detachment. `ingest_epoch` only
        # reads `inbox.id` and `inbox.name`, so no re-attach is needed.
        last_article_date, list_address = session.execute(
            select(Inbox.last_article_date, Inbox.list_address).where(
                Inbox.id == inbox.id
            )
        ).one()
        was_empty = last_article_date is None
        # Detect a new inbox's list address before its first walk (#855).
        # The inboxes walked before this one have had their chance to
        # promote, so its own address is the only one the walk's picks
        # could miss. Without it, cross-posts pinned to whichever inbox
        # promoted first, lkml since #766.
        if was_empty and list_address is None:
            tally = _sample_list_addresses(Path(inbox.mirror_path))
            if tally:
                _submit_promote_list_address(writer, inbox.id, tally).result(timeout=30)
                # Release the read transaction, as `flush_batch` does, so
                # each epoch's address map sees the promotion. Under the
                # sqlite3 driver's default (legacy) transaction control a
                # SELECT opens none, so today this is a safeguard only.
                session.commit()
        for epoch_path in discover_epochs(Path(inbox.mirror_path)):
            if remaining is not None and remaining <= 0:
                break
            r = ingest_epoch(
                session,
                inbox,
                epoch_path.name,
                epoch_path,
                writer=writer,
                limit=remaining,
                workers=workers,
            )
            results.append(r)
            if remaining is not None:
                remaining -= r.new + r.linked + r.dup_batch + r.dup_db + r.failed

    # Promote `Inbox.list_address` if we now have enough observations.
    # Dispatched as a WriteOp through the writer (gate semantics live in
    # `_submit_promote_list_address`). After a first walk this is the
    # fallback for a sample that could not promote, and the walk's pins
    # never saw the address.
    promoted = _submit_promote_list_address(writer, inbox.id).result(timeout=30)
    if was_empty and promoted is not None:
        logger.info(
            "%s: list address %s promoted after the first walk; run "
            "`admin canonicals backfill --reprocess --inbox %s` to re-pin "
            "its cross-posts",
            inbox.name,
            promoted,
            inbox.name,
        )

    # Refresh planner stats when we've moved enough rows that prior
    # `sqlite_stat1` can no longer be trusted, most importantly the
    # first ingest of a freshly-added inbox, which lands a whole archive
    # in one go and would otherwise leave the planner blind until the
    # next scheduled ANALYZE. Dispatched as a WriteOp; the label
    # `auto_analyze:<inbox>` matches the legacy write_transaction label
    # shape for slow-write WARNING correlation.
    threshold = settings.analyze_after_ingest_rows
    moved = sum(r.new + r.linked for r in results)
    if threshold > 0 and moved >= threshold:
        logger.info("auto-ANALYZE after %s/%d rows ingested", inbox.name, moved)
        _submit_analyze(writer, inbox.name).result(timeout=120)

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
    # which and why. Uses a plain SessionLocal (not the read pool)
    # because `cache.set` opens its own write session internally;
    # the read pool's query_only=1 sessions would reject the write.
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
