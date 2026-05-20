"""SQLite hygiene: ANALYZE and VACUUM (+ WAL checkpoint).

Both are intended for periodic cron runs. `analyze` is cheap and can
run while the web tier is up; `vacuum` holds an exclusive lock and
should run in a quiet window with no other DB writers.
"""
import sqlite3
import time
from pathlib import Path

import click
from sqlalchemy import text

from mimir.cli._common import _fmt_bytes
from mimir.extensions import engine, write_transaction


@click.command("analyze")
def analyze_command() -> None:
    """Run ANALYZE to refresh the SQLite query planner statistics.

    Stale `sqlite_stat1` makes the planner pick bad plans (we hit
    this once when the migration's ANALYZE ran on empty tables and
    later ingest left the stats wrong by orders of magnitude). Run
    after a big ingest delta, daily or weekly via cron is plenty.

    Example crontab (4:30am, after the daily VACUUM):

        30 4 * * * cd ~/Projects/python/mimir && poetry run flask --app mimir analyze
    """
    t0 = time.perf_counter()
    # Label the ANALYZE write so the slow-write WARNING attributes
    # the lock-hold cleanly: an operator correlating a slow broker
    # cache.set dispatch against the scheduler log will see
    # `label=analyze held=<N>ms` and know the cause.
    with write_transaction("analyze"):
        with engine.begin() as conn:
            conn.execute(text("ANALYZE"))
    elapsed = time.perf_counter() - t0
    click.echo(f"ANALYZE complete in {elapsed:.1f} s")


@click.command("vacuum")
def vacuum_command() -> None:
    """Reclaim space: VACUUM + WAL checkpoint.

    SQLite never reclaims freed pages on its own; the .db file grows
    past its actual content over time, and the WAL file grows during
    long ingests until something checkpoints it. This command runs
    `VACUUM` (rebuilds the database into a compact form) and
    `PRAGMA wal_checkpoint(TRUNCATE)` (collapses the WAL).

    VACUUM holds an exclusive lock for the duration and needs ~2× the
    on-disk size of free space. Run it during a quiet window, typical
    cadence is daily or weekly via cron, while ingest isn't active.

    Example crontab:

        0 4 * * * cd ~/Projects/python/mimir && poetry run flask --app mimir vacuum
    """
    db_path = Path(engine.url.database) if engine.url.database else None
    if db_path is None:
        raise click.ClickException("could not resolve DB path from engine URL")

    def _sizes() -> dict[str, int]:
        out: dict[str, int] = {}
        for suffix in ("", "-wal", "-shm"):
            p = db_path.with_name(db_path.name + suffix)
            out[suffix or "db"] = p.stat().st_size if p.exists() else 0
        return out

    before = _sizes()
    total_before = before["db"] + before["-wal"] + before["-shm"]
    click.echo(
        f"before: db={_fmt_bytes(before['db'])}  "
        f"wal={_fmt_bytes(before['-wal'])}  shm={_fmt_bytes(before['-shm'])}"
    )

    # `wal_checkpoint(TRUNCATE)` only succeeds when there are no
    # other readers, SQLAlchemy's connection pool keeps idle
    # connections that block it. Dispose the pool and run on a fresh
    # raw sqlite3 connection so we own the only handle. Any other
    # process that has the DB open (web server, warm-cache cron) will
    # also prevent the truncate; stop those before vacuuming.
    engine.dispose()

    t0 = time.perf_counter()
    conn = sqlite3.connect(str(db_path))
    try:
        # In WAL mode, VACUUM's full rebuild goes through the WAL, so
        # the WAL grows by ~db_size during the operation. Checkpoint
        # *after* to collapse it; the pre-checkpoint clears any
        # leftover WAL from prior writers so the truncate can run
        # cleanly when we're done.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    elapsed = time.perf_counter() - t0

    after = _sizes()
    total_after = after["db"] + after["-wal"] + after["-shm"]
    click.echo(
        f"after:  db={_fmt_bytes(after['db'])}  "
        f"wal={_fmt_bytes(after['-wal'])}  shm={_fmt_bytes(after['-shm'])}"
    )
    reclaimed = total_before - total_after
    click.echo(
        f"reclaimed {_fmt_bytes(reclaimed)} in {elapsed:.1f} s"
    )
