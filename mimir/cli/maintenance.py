"""SQLite hygiene: ANALYZE and VACUUM (+ WAL checkpoint).

Thin click wrappers that dispatch through the broker. The library
functions `mimir.maintenance.run_analyze` / `run_vacuum` do the
actual work inside the broker process; this module turns CLI
flags into RPC arguments and echoes the structured result.

Both are intended for periodic cron runs. `analyze` is cheap and
can run while the web tier is up; `vacuum` holds an exclusive lock
and pauses every other broker worker for the duration.
"""

import click

from mimir.cli._common import _fmt_bytes


@click.command("analyze")
@click.option(
    "--full",
    "full",
    is_flag=True,
    help="Override the connection's `PRAGMA analysis_limit` and run "
    "with `analysis_limit=0` (no cap) for this pass. Produces "
    "fully-accurate per-index distribution stats at the cost of "
    "a longer writer-lock hold (~25-30 s on the production-scale "
    "11M-row corpus). Use as the periodic safety-net pass; the "
    "default capped ANALYZE catches typical drift cheaply.",
)
def analyze_command(full: bool) -> None:
    """Run ANALYZE to refresh the SQLite query planner statistics.

    Stale `sqlite_stat1` makes the planner pick bad plans (we hit
    this once when the migration's ANALYZE ran on empty tables and
    later ingest left the stats wrong by orders of magnitude). Run
    after a big ingest delta, daily or weekly via cron is plenty.

    By default this runs with whatever `PRAGMA analysis_limit` the
    connection inherited from `mimir.extensions._sqlite_pragmas`
    (default 4000). Pass `--full` to override that for the duration
    of this pass and run with `analysis_limit=0` (no cap), which is
    the safety-net production needs once a week: the default cap
    means each daily pass undersamples a few tail-heavy indexes,
    and the full pass catches whatever drifted.
    """
    from mimir.broker.client import BrokerUnavailable, get_broker_client

    try:
        payload = get_broker_client().analyze(full=full)
    except BrokerUnavailable as exc:
        raise click.ClickException(f"broker analyze failed: {exc}")
    elapsed_s = payload.get("elapsed_ms", 0) / 1000.0
    kind = "full ANALYZE" if full else "ANALYZE"
    click.echo(f"{kind} complete in {elapsed_s:.1f} s")


@click.command("vacuum")
def vacuum_command() -> None:
    """Reclaim space: VACUUM + WAL checkpoint.

    SQLite never reclaims freed pages on its own; the .db file grows
    past its actual content over time, and the WAL file grows during
    long ingests until something checkpoints it. This command runs
    `VACUUM` (rebuilds the database into a compact form) and
    `PRAGMA wal_checkpoint(TRUNCATE)` (collapses the WAL).

    VACUUM holds an exclusive lock for the duration; every other
    broker worker pauses. Run during a quiet window.
    """
    from mimir.broker.client import BrokerUnavailable, get_broker_client

    try:
        payload = get_broker_client().vacuum()
    except BrokerUnavailable as exc:
        raise click.ClickException(f"broker vacuum failed: {exc}")
    _echo_vacuum_outcome(payload)


def _echo_vacuum_outcome(payload: dict) -> None:
    """Render the before/after/reclaimed lines from a VacuumResult dict."""
    before = payload.get("db_size_before", 0)
    after = payload.get("db_size_after", 0)
    reclaimed = payload.get("reclaimed", 0)
    elapsed_s = payload.get("elapsed_ms", 0) / 1000.0
    click.echo(f"before: total={_fmt_bytes(before)}")
    click.echo(f"after:  total={_fmt_bytes(after)}")
    click.echo(f"reclaimed {_fmt_bytes(reclaimed)} in {elapsed_s:.1f} s")
