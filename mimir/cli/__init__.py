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

The package is split by concern (see each submodule); names that
callers (tests, the legacy `mimir.cli` import path) used to import
from `mimir.cli` are re-exported here so the cutover is a pure
refactor.
"""
import click
from flask import Flask
from flask.cli import FlaskGroup

from mimir.cli._common import (
    _configure_logging,
    _fmt_bytes,
    _parse_pair,
    _select_inboxes,
)
from mimir.cli.admin import admin_group
from mimir.cli.admin.canonicals import (
    admin_canonicals_backfill_command,
    admin_canonicals_group,
)
from mimir.cli.admin.failures import (
    admin_failures_group,
    admin_failures_list_command,
    admin_failures_replay_command,
)
from mimir.cli.admin.inbox import (
    admin_inbox_add_command,
    admin_inbox_group,
    admin_inbox_list_command,
    admin_inbox_remove_command,
    admin_inbox_show_command,
    admin_inbox_trackers_add_command,
    admin_inbox_trackers_clear_command,
    admin_inbox_trackers_group,
    admin_inbox_trackers_remove_command,
    admin_inbox_trackers_set_command,
    admin_inbox_trackers_show_command,
    admin_inbox_update_command,
)
from mimir.cli.backfill import (
    backfill_article_files_command,
    backfill_article_trailers_command,
    backfill_patch_series_command,
)
from mimir.cli.cache import (
    WARM_CACHE_REFRESH_WITHIN_SEC,
    WARM_TOP_SUBSYSTEMS_PER_INBOX,
    warm_cache_command,
)
from mimir.cli.devseed import dev_seed_thread_command
from mimir.cli.ingest import (
    _push_indexnow,
    ingest_command,
    reindex_command,
    update_command,
)
from mimir.cli.initdb import init_db_command
from mimir.cli.mainline import update_mainline_command
from mimir.cli.maintenance import analyze_command, vacuum_command
from mimir.cli.show import show_command

__all__ = [
    "WARM_CACHE_REFRESH_WITHIN_SEC",
    "WARM_TOP_SUBSYSTEMS_PER_INBOX",
    "_configure_logging",
    "_fmt_bytes",
    "_parse_pair",
    "_push_indexnow",
    "_select_inboxes",
    "admin_canonicals_backfill_command",
    "admin_canonicals_group",
    "admin_failures_group",
    "admin_failures_list_command",
    "admin_failures_replay_command",
    "admin_group",
    "admin_inbox_add_command",
    "admin_inbox_group",
    "admin_inbox_list_command",
    "admin_inbox_remove_command",
    "admin_inbox_show_command",
    "admin_inbox_trackers_add_command",
    "admin_inbox_trackers_clear_command",
    "admin_inbox_trackers_group",
    "admin_inbox_trackers_remove_command",
    "admin_inbox_trackers_set_command",
    "admin_inbox_trackers_show_command",
    "admin_inbox_update_command",
    "analyze_command",
    "backfill_article_files_command",
    "backfill_article_trailers_command",
    "backfill_patch_series_command",
    "dev_seed_thread_command",
    "ingest_command",
    "init_db_command",
    "register_cli",
    "reindex_command",
    "show_command",
    "update_command",
    "update_mainline_command",
    "vacuum_command",
    "warm_cache_command",
]


def register_cli(app: Flask) -> None:
    app.cli.add_command(init_db_command)
    app.cli.add_command(ingest_command)
    app.cli.add_command(reindex_command)
    app.cli.add_command(show_command)
    app.cli.add_command(update_command)
    app.cli.add_command(update_mainline_command)
    app.cli.add_command(backfill_article_files_command)
    app.cli.add_command(backfill_article_trailers_command)
    app.cli.add_command(backfill_patch_series_command)
    app.cli.add_command(warm_cache_command)
    app.cli.add_command(vacuum_command)
    app.cli.add_command(analyze_command)
    app.cli.add_command(dev_seed_thread_command)
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
