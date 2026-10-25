"""CLI command package.

Two public entry points:

- `register_cli(app)`: attaches every command + group to the Flask
  app's `app.cli`. Used both by Flask's own `flask --app mimir <cmd>`
  discovery path and by the standalone `mimir` shell command.
- `mimir`: a `FlaskGroup`-based Click group exposed via Poetry as the
  `mimir` console script (`[tool.poetry.scripts]` in pyproject.toml).
  Same backing as `flask --app mimir` since both use `FlaskGroup`'s
  app-factory discovery; this just spares operators the
  `flask --app mimir` prefix.

The package is split by concern (see each submodule). Import a command
from its own module; this file imports only what `register_cli` wires.
"""

import click
from flask import Flask
from flask.cli import FlaskGroup

from mimir.cli.admin import admin_group
from mimir.cli.backfill import (
    backfill_article_files_command,
    backfill_article_trailers_command,
    backfill_patch_series_command,
    backfill_series_chains_command,
    backfill_thread_roots_command,
)
from mimir.cli.bootstrap import bootstrap_inboxes_command
from mimir.cli.broker import broker_command, broker_ping_command
from mimir.cli.cache import warm_cache_command
from mimir.cli.devseed import dev_seed_thread_command
from mimir.cli.diagnostics import tracemalloc_diff_command
from mimir.cli.doctor import doctor_command
from mimir.cli.ingest import ingest_command, reindex_command, update_command
from mimir.cli.mainline import update_mainline_command
from mimir.cli.maintenance import analyze_command, vacuum_command
from mimir.cli.show import show_command


def register_cli(app: Flask) -> None:
    app.cli.add_command(bootstrap_inboxes_command)
    app.cli.add_command(broker_command)
    app.cli.add_command(broker_ping_command)
    app.cli.add_command(ingest_command)
    app.cli.add_command(reindex_command)
    app.cli.add_command(show_command)
    app.cli.add_command(update_command)
    app.cli.add_command(update_mainline_command)
    app.cli.add_command(backfill_article_files_command)
    app.cli.add_command(backfill_article_trailers_command)
    app.cli.add_command(backfill_patch_series_command)
    app.cli.add_command(backfill_series_chains_command)
    app.cli.add_command(backfill_thread_roots_command)
    app.cli.add_command(warm_cache_command)
    app.cli.add_command(vacuum_command)
    app.cli.add_command(analyze_command)
    app.cli.add_command(dev_seed_thread_command)
    app.cli.add_command(doctor_command)
    app.cli.add_command(tracemalloc_diff_command)
    app.cli.add_command(admin_group)


def _create_app_for_cli(info=None):
    """Factory hook for the standalone `mimir` Click group. `FlaskGroup`
    passes a `ScriptInfo` instance, which we ignore (no per-invocation
    config), and we return the same app `create_app()` builds. The
    import is local to dodge a top-of-module circular: `mimir.cli` is
    imported by `mimir.__init__` via `register_cli` during `create_app`."""
    from mimir import create_app

    return create_app()


@click.group(cls=FlaskGroup, create_app=_create_app_for_cli)
def mimir() -> None:
    """mimir CLI, every command registered via `register_cli` is
    available as `mimir <command>`. Same backing as
    `flask --app mimir <command>`; both invocations stay supported."""
