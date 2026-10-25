"""SQLite hygiene operations (ANALYZE + VACUUM).

Both are periodic-cron writers: ANALYZE refreshes `sqlite_stat1`
so the planner picks correct join shapes; VACUUM compacts the file
and collapses the WAL. They're orchestration helpers, not CLI
wrappers, the click commands in `mimir.cli.maintenance` delegate
here, and the Phase 2.3 broker handlers do too. Single-concern:
"the SQLite hygiene writers, end to end."

ANALYZE reads every row and is not cheap (CONTEXT.md "ANALYZE sample
size"). That matters beyond tidiness: the post-migrate ANALYZE runs
before the broker touches its healthcheck sentinel, so it is on the
startup budget. VACUUM holds an
exclusive lock for the duration (minutes on large databases); under
the broker that means every other broker worker pauses, so it
should run in a quiet window only.
"""

import logging
import sqlite3
import time
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import text

from mimir.extensions import engine

logger = logging.getLogger(__name__)


class AnalyzeResult(BaseModel):
    """Outcome of one `run_analyze` invocation."""

    elapsed_ms: int


class VacuumResult(BaseModel):
    """Outcome of one `run_vacuum` invocation.

    All three size numbers are the DATABASE's logical size
    (`page_count * page_size`), so `before - after == reclaimed`
    exactly and an operator can derive any from the others. They are
    deliberately NOT the on-disk footprint: see `run_vacuum` for why
    the footprint is the wrong basis for this, and `_checkpoint_truncate`
    for what `wal_truncated` means. When it is False the footprint
    temporarily exceeds these numbers by roughly the database size,
    which is logged rather than returned."""

    elapsed_ms: int
    db_size_before: int
    db_size_after: int
    reclaimed: int
    wal_truncated: bool = True


def run_analyze() -> AnalyzeResult:
    """Refresh SQLite planner statistics, every row sampled (see
    CONTEXT.md "ANALYZE sample size").

    Phase 6a dual-dispatch: when a broker WriterThread is active
    (steady-state `handle_analyze` RPC), submit the ANALYZE as a
    WriteOp so it serialises through the single writer. When no writer
    is active (the pre-serve `_post_migrate_analyze_if_needed`, or
    non-broker callers / tests), fall back to the shared-engine
    `engine.begin()` path."""
    t0 = time.perf_counter()

    try:
        from mimir.broker import _context

        writer = _context.get_active_writer()
    except RuntimeError:
        writer = None

    if writer is not None:
        from mimir.broker.writes import WriteOp

        writer.submit(
            WriteOp(label="analyze", fn=lambda conn: conn.execute(text("ANALYZE")))
        ).result()
    else:
        with engine.begin() as conn:
            conn.execute(text("ANALYZE"))

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return AnalyzeResult(elapsed_ms=elapsed_ms)


def _db_sizes() -> dict[str, int]:
    """Return the byte sizes of the main DB file and its -wal/-shm
    siblings. Used by `run_vacuum` for the before/after diff that
    operators see in the result."""
    db_path = Path(engine.url.database) if engine.url.database else None
    if db_path is None:
        raise RuntimeError("could not resolve DB path from engine URL")
    out: dict[str, int] = {}
    for suffix in ("", "-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix)
        out[suffix or "db"] = p.stat().st_size if p.exists() else 0
    return out


def _logical_size(conn: sqlite3.Connection) -> int:
    """The database's own view of its size, in bytes.

    `page_count * page_size` read on the vacuuming connection. This is
    the only basis that answers "how much did the VACUUM free" on a
    live deployment, and the reason is checkpoint ordering: in WAL mode
    VACUUM writes the rebuilt database INTO THE WAL, and the main file
    is only rewritten when a checkpoint copies it across, which cannot
    advance past the oldest reader's snapshot. With any reader
    connected, the main file therefore does not move at all.

    Measured on a 200k-row throwaway DB with a reader parked
    (2026-09-26): main file 91,258,880 -> 91,258,880 while the true
    reclaim was 82,120,704, which `page_count` reports immediately.
    Freelist pages count toward `page_count`, so this is
    checkpoint-independent in both directions.
    """
    pc = conn.execute("PRAGMA page_count").fetchone()[0]
    ps = conn.execute("PRAGMA page_size").fetchone()[0]
    return int(pc) * int(ps)


def _checkpoint_truncate(conn: sqlite3.Connection, phase: str) -> bool:
    """Run `wal_checkpoint(TRUNCATE)` and report whether it actually ran.

    The pragma returns `(busy, log, checkpointed)` and is a NO-OP
    returning `busy=1` whenever another connection has the database
    open. It never raises, so an unchecked call is silently ignored,
    which is what shipped: on a live deployment `ct-mimir-web` and
    `ct-mimir-tasks` hold `query_only` connections continuously, so the
    post-VACUUM truncate never ran and the WAL stayed at ~database size
    until the next container restart (observed 2026-08-03: 16.7 GB of
    WAL still present 26 minutes after a VACUUM).

    The old docstring justified the call with "the broker satisfies
    this by being the sole writer". True, and the wrong predicate:
    a truncate needs the sole CONNECTION, and readers block it.
    """
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    busy = bool(row and row[0])
    if busy:
        logger.warning(
            "vacuum: %s wal_checkpoint(TRUNCATE) could not run (busy); "
            "another connection holds the database open, so the WAL stays "
            "at roughly database size until every connection closes",
            phase,
        )
    return not busy


def run_vacuum() -> VacuumResult:
    """Compact the database via `VACUUM` and collapse the WAL via
    `PRAGMA wal_checkpoint(TRUNCATE)`. Two checkpoints: one before
    so any leftover WAL clears first, one after to truncate the
    WAL VACUUM itself wrote into.

    SQLAlchemy's connection pool keeps idle connections that block
    the post-truncate. Dispose the engine first so we own the only
    handle, then re-use a raw `sqlite3` connection for the VACUUM
    itself. Any other process that has the DB open will also
    prevent the truncate; the operator-facing contract is "run
    when no other process is writing", which the broker satisfies
    by being the sole writer."""
    db_path = Path(engine.url.database) if engine.url.database else None
    if db_path is None:
        raise RuntimeError("could not resolve DB path from engine URL")

    engine.dispose()

    t0 = time.perf_counter()
    conn = sqlite3.connect(str(db_path))
    try:
        logical_before = _logical_size(conn)
        # In WAL mode, VACUUM's full rebuild goes through the WAL,
        # so the WAL grows by ~db_size during the operation.
        # Checkpoint *after* to collapse it; the pre-checkpoint
        # clears any leftover WAL from prior writers so the
        # truncate can run cleanly when we're done.
        _checkpoint_truncate(conn, "pre-vacuum")
        conn.execute("VACUUM")
        wal_truncated = _checkpoint_truncate(conn, "post-vacuum")
        logical_after = _logical_size(conn)
    finally:
        conn.close()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    # Logical, not on-disk. Two wrong answers were shipped before this:
    # summing main+wal+shm reported a large NEGATIVE reclaim (production
    # logged -16,720,121,472 on 2026-08-03, because the WAL had just
    # been inflated by the rebuild), and diffing the main file alone
    # reported exactly ZERO (because with any reader connected the main
    # file never moves). Both are the same defect, a figure whose basis
    # is not the one its label implies, with different signs.
    reclaimed = logical_before - logical_after

    if not wal_truncated:
        # The footprint is what fills the volume, so surface it where an
        # operator chasing disk will look, without putting a second
        # basis into the returned triple.
        disk = _db_sizes()
        logger.info(
            "vacuum: reclaimed %d bytes logically; on-disk footprint is "
            "%d bytes (db %d + wal %d) until a checkpoint can run",
            reclaimed,
            disk["db"] + disk["-wal"] + disk["-shm"],
            disk["db"],
            disk["-wal"],
        )

    return VacuumResult(
        elapsed_ms=elapsed_ms,
        db_size_before=logical_before,
        db_size_after=logical_after,
        reclaimed=reclaimed,
        wal_truncated=wal_truncated,
    )


__all__ = [
    "AnalyzeResult",
    "VacuumResult",
    "run_analyze",
    "run_vacuum",
]
