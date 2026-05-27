"""`update-mainline`: sync Linus's `linux.git` and (re)build the
MAINTAINERS-derived schema + the lore `Link:`-trailer index.

Thin click wrapper that dispatches through the broker. The heavy
lifting (clone/fetch, MAINTAINERS reparse, Link-trailer walk)
lives in `mimir.mainline.update_mainline` and runs inside the
broker's single-writer process; this command turns CLI flags
into RPC arguments and echoes the structured result.
"""

import click

from mimir.cli._common import _configure_logging


@click.command("update-mainline")
@click.option(
    "--skip-fetch",
    is_flag=True,
    help="Don't `git fetch` the mainline mirror; just re-read the local HEAD.",
)
@click.option(
    "--skip-maintainers",
    is_flag=True,
    help="Don't re-parse MAINTAINERS; only walk new commits for Link: trailers.",
)
@click.option(
    "--skip-commits",
    is_flag=True,
    help="Don't walk commits for Link: trailers; only reload MAINTAINERS.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Re-parse MAINTAINERS and replace subsystems even if HEAD hasn't moved.",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="-v: progress detail. -vv: debug.",
)
def update_mainline_command(
    skip_fetch: bool,
    skip_maintainers: bool,
    skip_commits: bool,
    force: bool,
    verbose: int,
) -> None:
    """Sync the mainline kernel tree (Linus's `linux.git`) and load
    its MAINTAINERS file + index `Link:` trailers from commit
    messages.

    Clones the tree on first run, fetches on subsequent runs. Two
    independent passes against the resulting tree:

    1. MAINTAINERS: read the file at HEAD, replace the
       `subsystems` / `subsystem_paths` / `subsystem_maintainers`
       tables transactionally. Skipped when HEAD hasn't moved
       since the last load (`--force` overrides; for re-runs
       after a parser fix). `--skip-maintainers` disables this
       pass entirely for the tick.

    2. Commit Link-trailer walk: scan every commit since the last
       walker cursor for `Link: https://lore.kernel.org/.../<msgid>`
       trailers, insert into `mainline_commits`. Resumable; the
       second cursor on `MainlineState` advances monotonically.
       First run walks the full history (slow); subsequent ticks
       only see new commits. `--skip-commits` disables this pass.

    The two passes have independent cursors because MAINTAINERS
    only changes when that one file does, but commit-walker has
    new work on almost every tick.
    """
    _configure_logging(verbose)

    from mimir.broker.client import BrokerUnavailable, get_broker_client

    try:
        payload = get_broker_client().update_mainline(
            skip_fetch=skip_fetch,
            skip_maintainers=skip_maintainers,
            skip_commits=skip_commits,
            force=force,
        )
    except BrokerUnavailable as exc:
        # update_mainline() catches per-tree exceptions; FileNotFoundError
        # from missing tree dirs surfaces in result.trees[slug].error.
        raise click.ClickException(f"broker update_mainline failed: {exc}")
    _echo_update_mainline_outcome(payload)
    # Signal failure to the calling process (systemd timer, cron) so
    # operators running periodic ticks get an alert when any tree failed
    # rather than a silent zero exit with an error buried in the output.
    if any(not tr.get("ok", True) for tr in (payload.get("trees") or {}).values()):
        raise click.exceptions.Exit(1)


def _echo_update_mainline_outcome(payload: dict) -> None:
    """Render the structured `UpdateMainlineResult` to operator-
    facing lines. Same shape whether the call went via broker (dict
    payload from RPC) or direct (model dump). State-change lines
    only; steady-state ticks stay silent.

    Per-tree failures are surfaced as Error lines so the operator
    sees them in the terminal output even when the overall RPC
    succeeded. A misconfigured tree (wrong URL, missing MAINTAINERS
    blob) should not silently disappear into the broker log."""
    head = payload.get("mainline_head") or ""
    head_short = head[:12]
    if payload.get("maintainers_ran"):
        click.echo(
            f"update-mainline: loaded {payload.get('subsystems_loaded', 0)} "
            f"subsystems from linus@{head_short}"
        )
    elif payload.get("maintainers_unchanged") and head_short:
        click.echo(
            f"update-mainline: MAINTAINERS unchanged (HEAD {head_short}); "
            "use --force to re-parse"
        )
    if payload.get("commits_ran") and payload.get("commits_seen"):
        click.echo(
            f"update-mainline: walked {payload['commits_seen']} commits "
            f"({payload.get('commits_linked', 0)} with lore Link:, "
            f"{payload.get('rows_inserted', 0)} rows indexed)"
        )
    # Surface per-tree failures so the operator learns which tree
    # failed rather than silently getting zero output for that tree.
    for slug, tree_result in (payload.get("trees") or {}).items():
        if not tree_result.get("ok", True):
            err = tree_result.get("error") or "unknown error"
            click.echo(f"update-mainline: tree {slug} failed: {err}", err=True)
