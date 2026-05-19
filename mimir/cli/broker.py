"""`mimir broker` and `mimir broker-ping` CLI commands.

`broker` launches the write-broker daemon (blocks until
SIGTERM/SIGINT). Wired into the scheduler-sidecar compose service
in production; locally, run by hand for an end-to-end smoke test.

`broker-ping` is the compose healthcheck command: open the socket,
send `{"op":"ping"}`, exit 0 on `{"ok":true}` or non-zero on
anything else.
"""
from pathlib import Path

import click

from mimir.broker.client import BrokerClient, BrokerUnavailable
from mimir.broker.server import serve
from mimir.cli._common import _configure_logging


@click.command("broker")
@click.option(
    "--socket", "socket_path",
    required=True, type=click.Path(path_type=Path),
    help="UNIX socket path to listen on (e.g. /data/.broker.sock).",
)
@click.option("--verbose", "-v", count=True, help="-v: info logs, -vv: debug.")
def broker_command(socket_path: Path, verbose: int) -> None:
    """Run the write-broker daemon. Blocks until terminated.

    Owns the sole writer connection to the cache table; clients (web
    tier, scheduler-tier CLI commands) submit `cache.set` / `delete`
    / `delete_for_inbox` / `purge_expired` over the socket. The
    broker also runs a periodic purge thread internally.
    """
    _configure_logging(verbose)
    serve(socket_path)


@click.command("broker-ping")
@click.option(
    "--socket", "socket_path",
    required=True, type=click.Path(path_type=Path),
    help="UNIX socket path of a running broker.",
)
def broker_ping_command(socket_path: Path) -> None:
    """Probe a running broker. Exits 0 on success, 1 otherwise.
    Used as the compose healthcheck for `mimir-broker`."""
    client = BrokerClient(socket_path)
    try:
        ok = client.ping()
    except BrokerUnavailable as exc:
        click.echo(f"broker ping failed: {exc}", err=True)
        raise SystemExit(1)
    finally:
        client.close()
    if not ok:
        click.echo("broker ping returned ok=false", err=True)
        raise SystemExit(1)
    click.echo("ok")
