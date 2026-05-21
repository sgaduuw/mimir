"""`update-mainline`: sync Linus's `linux.git` and (re)build the
MAINTAINERS-derived schema + the lore `Link:`-trailer index.

Thin click wrapper around `mimir.mainline.update_mainline`. The
heavy lifting (clone/fetch, MAINTAINERS reparse, Link-trailer
walk) lives in the library module so the broker handler can call
the same code path; this command just turns CLI flags into
function args and echoes the structured result.

Broker dispatch (Phase 2.3): when `BROKER_SOCKET_PATH` is set the
RPC routes through the broker (which then calls the same library
function inside the single-writer process). Falls back to the
direct path when broker mode is off.
"""

import click

from mimir.cli._common import _configure_logging
from mimir.config import settings


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

    if settings.broker_socket_path is not None:
        # Broker mode: dispatch via RPC. The broker calls the same
        # `update_mainline` library function inside its single-
        # writer process, keeping the subsystems / mainline_commits
        # writes out of cross-process contention.
        from mimir.broker.client import BrokerUnavailable, get_broker_client

        try:
            payload = get_broker_client().update_mainline(
                skip_fetch=skip_fetch,
                skip_maintainers=skip_maintainers,
                skip_commits=skip_commits,
                force=force,
            )
        except BrokerUnavailable as exc:
            raise click.ClickException(f"broker update_mainline failed: {exc}")
        _echo_update_mainline_outcome(payload)
        return

    from mimir.mainline import update_mainline

    try:
        result = update_mainline(
            skip_fetch=skip_fetch,
            skip_maintainers=skip_maintainers,
            skip_commits=skip_commits,
            force=force,
        )
    except FileNotFoundError as exc:
        # `load_maintainers` raises this when the tree has no
        # MAINTAINERS file at HEAD (operator pointed at the wrong
        # tree). Translate to a ClickException so the CLI exits
        # with a clean message instead of a traceback.
        raise click.ClickException(str(exc))
    _echo_update_mainline_outcome(result.model_dump(mode="json"))


def _echo_update_mainline_outcome(payload: dict) -> None:
    """Render the structured `UpdateMainlineResult` to operator-
    facing lines. Same shape whether the call went via broker (dict
    payload from RPC) or direct (model dump). State-change lines
    only; steady-state ticks stay silent."""
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
