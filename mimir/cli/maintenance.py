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
def analyze_command() -> None:
    """Run ANALYZE to refresh the SQLite query planner statistics.

    Stale `sqlite_stat1` makes the planner pick bad plans (we hit
    this once when the migration's ANALYZE ran on empty tables and
    later ingest left the stats wrong by orders of magnitude). Run
    after a big ingest delta, weekly via cron is plenty.

    Every row is sampled; see CONTEXT.md "ANALYZE sample size".
    """
    from mimir.broker.client import BrokerUnavailable, get_broker_client
    from mimir.broker.protocol import AnalyzeRequest

    try:
        payload = get_broker_client().call(AnalyzeRequest())
    except BrokerUnavailable as exc:
        raise click.ClickException(f"broker analyze failed: {exc}")
    elapsed_s = payload.get("elapsed_ms", 0) / 1000.0
    click.echo(f"ANALYZE complete in {elapsed_s:.1f} s")


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
    from mimir.broker.protocol import VacuumRequest

    try:
        payload = get_broker_client().call(VacuumRequest())
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
    if not payload.get("wal_truncated", True):
        # The operator-visible half of the same finding: without this
        # line the only symptom is a volume that quietly holds twice
        # the database until the next restart.
        click.echo(
            "note: the WAL could not be truncated (another connection "
            "holds the database open), so it stays at roughly database "
            "size until every connection closes"
        )
