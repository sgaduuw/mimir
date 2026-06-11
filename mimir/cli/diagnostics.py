"""Broker memory diagnostic commands.

Counterpart to the broker's tracemalloc snapshotter
(_maybe_start_tracemalloc_snapshotter in mimir/broker/server.py).
Both ship together as latent diagnostic capability; both are no-ops
unless TRACEMALLOC_INTERVAL_SECONDS is set on the broker.

See _claude/specs/2026-06-11-broker-tracemalloc-diagnostic-design.md.
"""

import pickle
import tracemalloc
from pathlib import Path

import click


def _load(path: Path) -> tracemalloc.Snapshot:
    """Load a tracemalloc Snapshot pickle, with a useful Click error
    on corrupt or non-pickle input."""
    try:
        with open(path, "rb") as f:
            snap = pickle.load(f)
    except (pickle.UnpicklingError, EOFError, OSError) as e:
        raise click.BadParameter(f"could not load snapshot from {path}: {e}") from e
    if not isinstance(snap, tracemalloc.Snapshot):
        raise click.BadParameter(
            f"{path} is not a tracemalloc Snapshot pickle (got {type(snap).__name__})"
        )
    return snap


@click.command("tracemalloc-diff")
@click.argument(
    "snapshot_a",
    type=click.Path(exists=True, path_type=Path, dir_okay=False),
)
@click.argument(
    "snapshot_b",
    type=click.Path(exists=True, path_type=Path, dir_okay=False),
)
@click.option("--top", default=25, help="Number of growers to show.")
@click.option(
    "--filter-prefix",
    default=None,
    help=(
        "Show only allocations from files starting with this prefix "
        "(e.g. /app/mimir to scope to mimir source)."
    ),
)
def tracemalloc_diff_command(
    snapshot_a: Path,
    snapshot_b: Path,
    top: int,
    filter_prefix: str | None,
) -> None:
    """Diff two tracemalloc Snapshot pickles, print top growers.

    SNAPSHOT_A is the earlier snapshot, SNAPSHOT_B the later. Output
    is ranked by (B.size - A.size) descending. Filter and table
    formatting fill in by the next plan tasks.
    """
    snap_a = _load(snapshot_a)
    snap_b = _load(snapshot_b)
    stats = snap_b.compare_to(snap_a, "lineno")[:top]
    click.echo("mimir tracemalloc-diff")
    click.echo(f"  a: {snapshot_a.name}")
    click.echo(f"  b: {snapshot_b.name}")
    click.echo("")
    click.echo(f"  {'SIZE DIFF':>12}  {'COUNT DIFF':>10}  LOCATION")
    for stat in stats:
        size_mib = stat.size_diff / (1024 * 1024)
        sign = "+" if size_mib >= 0 else "-"
        size_str = f"{sign}{abs(size_mib):.1f} MiB"
        count_str = f"{stat.count_diff:+,}"
        # tracemalloc.Traceback is sorted oldest -> most recent.
        # [-1] is the allocation site (what the operator wants to see
        # first); the 3 prior frames are the callers, walked most-
        # recent-first so the trail reads top-down toward the outer
        # context.
        frames = list(stat.traceback)
        leader = frames[-1]
        loc = f"{leader.filename}:{leader.lineno}"
        click.echo(f"  {size_str:>12}  {count_str:>10}  {loc}")
        # Show up to 3 frames of caller context, most-recent caller first
        for frame in reversed(frames[-4:-1]):
            click.echo(f"  {'':>12}  {'':>10}    {frame.filename}:{frame.lineno}")
