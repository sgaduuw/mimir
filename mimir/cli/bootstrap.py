"""`bootstrap-inboxes`: seed the `inboxes` table from `Settings.inboxes`.

Post-2.0.0 the broker is the sole SQLite writer process; this CLI
command always routes the work via the broker daemon. Useful for
operator-triggered re-bootstrap (re-reading env config into the
DB without restarting the broker). The broker itself also runs
bootstrap as part of its self-startup sequence (gated by the
`/data/.bootstrapped` sentinel), so a routine container restart
doesn't need this command.
"""

import click


@click.command("bootstrap-inboxes")
def bootstrap_inboxes_command() -> None:
    """Seed `inboxes` from env config. Idempotent; safe to re-run."""
    from mimir.broker.client import BrokerUnavailable, get_broker_client

    try:
        count = get_broker_client().bootstrap_inboxes()
    except BrokerUnavailable as exc:
        raise click.ClickException(f"broker bootstrap_inboxes failed: {exc}")
    click.echo(f"bootstrap-inboxes: {count} inbox(es) reconciled (via broker)")
