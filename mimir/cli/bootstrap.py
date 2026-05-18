"""`bootstrap-inboxes` — seed the `inboxes` table from `Settings.inboxes`.

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
"""
import click

from mimir.inboxes import bootstrap_inboxes


@click.command("bootstrap-inboxes")
def bootstrap_inboxes_command() -> None:
    """Seed `inboxes` from env config. Idempotent; safe to re-run."""
    inboxes = bootstrap_inboxes()
    click.echo(f"bootstrap-inboxes: {len(inboxes)} inbox(es) reconciled")
