"""`bootstrap-inboxes`: seed the `inboxes` table from `Settings.inboxes`.

Run once on every scheduler-sidecar start, right after `alembic upgrade
head` and before the `/data/.migrated` healthcheck sentinel is touched.
That ordering means the web container starts only after env-configured
inboxes exist in the DB, so a first request to `/` doesn't see an empty
meta-index.

The bootstrap is intentionally separate from `create_app()`: the web
tier must not write to the DB at startup. With `READ_ONLY_DB=true` on
the web container, a startup write would fail outright; without it the
write is harmless but architecturally wrong (writes belong on the
sidecar, alongside migrations). Idempotent via `ON CONFLICT (name) DO
NOTHING`; admin edits to existing rows are never clobbered.

Broker dispatch (Phase 2.0): when `BROKER_SOCKET_PATH` is set, this
command no longer writes the DB directly. Instead it sends the
`bootstrap_inboxes` RPC to the broker, which performs the upserts
inside the broker process's writer connection. Falls back to the
direct path when broker mode is off (matches the cache module's
shape). This is the canary migration for the long-op family; the
remaining long ops follow in Phase 2.1+.
"""
import click

from mimir.config import settings
from mimir.inboxes import bootstrap_inboxes


@click.command("bootstrap-inboxes")
def bootstrap_inboxes_command() -> None:
    """Seed `inboxes` from env config. Idempotent; safe to re-run."""
    if settings.broker_socket_path is not None:
        # Broker mode: long ops route through the broker so all writes
        # serialise on one process. The broker handler delegates to
        # the same `mimir.inboxes.bootstrap_inboxes()` we'd call
        # locally, just from inside the broker process.
        from mimir.broker.client import BrokerUnavailable, get_broker_client
        try:
            count = get_broker_client().bootstrap_inboxes()
        except BrokerUnavailable as exc:
            # Hard fail: bootstrap-inboxes is a scheduler startup
            # step; the web tier gates on /data/.migrated and will
            # see an empty inboxes table if we silently swallow.
            raise click.ClickException(
                f"broker bootstrap_inboxes failed: {exc}"
            )
        click.echo(
            f"bootstrap-inboxes: {count} inbox(es) reconciled (via broker)"
        )
        return
    inboxes = bootstrap_inboxes()
    click.echo(f"bootstrap-inboxes: {len(inboxes)} inbox(es) reconciled")
