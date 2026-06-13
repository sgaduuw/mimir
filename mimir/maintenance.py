"""SQLite hygiene operations (ANALYZE + VACUUM).

Both are periodic-cron writers: ANALYZE refreshes `sqlite_stat1`
so the planner picks correct join shapes; VACUUM compacts the file
and collapses the WAL. They're orchestration helpers, not CLI
wrappers, the click commands in `mimir.cli.maintenance` delegate
here, and the Phase 2.3 broker handlers do too. Single-concern:
"the SQLite hygiene writers, end to end."

ANALYZE is cheap (~1-3 s on the production 11M-row corpus with
`analysis_limit=4000` per `_sqlite_pragmas`). VACUUM holds an
exclusive lock for the duration (minutes on large databases); under
the broker that means every other broker worker pauses, so it
should run in a quiet window only.
"""

import sqlite3
import time
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import text

from mimir.config import settings
from mimir.extensions import engine


class AnalyzeResult(BaseModel):
    """Outcome of one `run_analyze` invocation. `full` reflects the
    flag passed in (so a caller examining the result can tell which
    kind of pass ran)."""

    full: bool
    elapsed_ms: int


class VacuumResult(BaseModel):
    """Outcome of one `run_vacuum` invocation. The size fields are
    the sum of the main DB file plus its `-wal` and `-shm`
    siblings (cleanup is most visible there)."""

    elapsed_ms: int
    db_size_before: int
    db_size_after: int
    reclaimed: int


def run_analyze(*, full: bool = False) -> AnalyzeResult:
    """Refresh SQLite planner statistics.

    By default uses whatever `PRAGMA analysis_limit` the connection
    inherited from `_sqlite_pragmas` (4000 in production). `full=True`
    overrides that to 0 for the duration of this pass so the
    weekly safety-net catches index distributions the default
    bounded sample undersamples (the 1.36.4 calibration).

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

        label = "analyze_full" if full else "analyze"
        default_limit = settings.analyze_limit

        def _fn(conn):
            if full:
                # Override the persistent writer connection's limit for
                # this pass, then restore the default. The writer reuses
                # one connection across ops and never reconnects, so
                # unlike the shared engine (which re-applies the default
                # on every connect via _sqlite_pragmas) we MUST reset it
                # here or the next bounded analyze runs unbounded.
                conn.execute(text("PRAGMA analysis_limit=0"))
            try:
                conn.execute(text("ANALYZE"))
            finally:
                if full:
                    conn.execute(text(f"PRAGMA analysis_limit={default_limit}"))

        writer.submit(WriteOp(label=label, fn=_fn)).result()
    else:
        with engine.begin() as conn:
            if full:
                # Shared engine re-applies analysis_limit on the next
                # connect via _sqlite_pragmas, so no explicit reset is
                # needed on this path.
                conn.execute(text("PRAGMA analysis_limit=0"))
            conn.execute(text("ANALYZE"))

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return AnalyzeResult(full=full, elapsed_ms=elapsed_ms)


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

    before = _db_sizes()
    total_before = before["db"] + before["-wal"] + before["-shm"]

    engine.dispose()

    t0 = time.perf_counter()
    conn = sqlite3.connect(str(db_path))
    try:
        # In WAL mode, VACUUM's full rebuild goes through the WAL,
        # so the WAL grows by ~db_size during the operation.
        # Checkpoint *after* to collapse it; the pre-checkpoint
        # clears any leftover WAL from prior writers so the
        # truncate can run cleanly when we're done.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    after = _db_sizes()
    total_after = after["db"] + after["-wal"] + after["-shm"]
    reclaimed = total_before - total_after

    return VacuumResult(
        elapsed_ms=elapsed_ms,
        db_size_before=total_before,
        db_size_after=total_after,
        reclaimed=reclaimed,
    )


__all__ = [
    "AnalyzeResult",
    "VacuumResult",
    "run_analyze",
    "run_vacuum",
]
