"""`mimir broker` and `mimir broker-ping` CLI commands.

`broker` launches the write-broker daemon (blocks until
SIGTERM/SIGINT). Wired into the scheduler-sidecar compose service
in production; locally, run by hand for an end-to-end smoke test.

`broker-ping` probes a running broker: exit 0 on an `ok` reply, 1 on
anything else. It delegates to the stdlib-only `mimir_broker_ping`,
which the compose healthcheck calls directly as `mimir-broker-ping`
so it skips this package's import (#758).
"""

from pathlib import Path

import click

import mimir_broker_ping
from mimir.broker.server import serve
from mimir.cli._common import _configure_logging


@click.command("broker")
@click.option(
    "--socket",
    "socket_path",
    required=True,
    type=click.Path(path_type=Path),
    help="UNIX socket path to listen on (e.g. /data/.broker.sock).",
)
@click.option("--verbose", "-v", count=True, help="-v: debug logs (per-RPC).")
def broker_command(socket_path: Path, verbose: int) -> None:
    """Run the write-broker daemon. Blocks until terminated.

    Owns the sole writer connection to the cache table; clients (web
    tier, scheduler-tier CLI commands) submit `cache.set` / `delete`
    / `delete_for_inbox` / `purge_expired` over the socket. The
    broker also runs a periodic purge thread internally.

    Log floor is INFO regardless of `-v` so the daemon's startup,
    shutdown, and periodic-purge lines are always visible; without
    that an operator can't tell whether the container is actually
    running (silent WARNING-level default looks identical to a
    crashed loop). `-v` adds per-RPC DEBUG logs.
    """
    _configure_logging(max(verbose, 1))
    serve(socket_path)


@click.command("broker-ping")
@click.option(
    "--socket",
    "socket_path",
    required=True,
    type=click.Path(path_type=Path),
    help="UNIX socket path of a running broker.",
)
def broker_ping_command(socket_path: Path) -> None:
    """Probe a running broker. Exits 0 on success, 1 otherwise.
    The healthcheck uses `mimir-broker-ping`, which skips the app import."""
    raise SystemExit(mimir_broker_ping.main(["--socket", str(socket_path)]))
