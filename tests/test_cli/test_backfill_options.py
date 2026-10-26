"""The backfill commands share one option set (`backfill_options`); a pin
so an edit to one command cannot drift from the rest."""

import click
import pytest

from mimir.cli.admin.canonicals import admin_canonicals_backfill_command
from mimir.cli.backfill import (
    backfill_article_files_command,
    backfill_article_trailers_command,
    backfill_patch_series_command,
)

_COMMANDS = {
    "backfill-article-files": backfill_article_files_command,
    "backfill-article-trailers": backfill_article_trailers_command,
    "backfill-patch-series": backfill_patch_series_command,
    "admin-canonicals-backfill": admin_canonicals_backfill_command,
}

# `--reprocess` names what gets redone, so its help is per command.
_SHARED = ("limit", "restart", "yes", "verbose")


def _option(command: click.Command, name: str) -> dict:
    param = next(p for p in command.params if p.name == name)
    return {
        "opts": param.opts,
        "type": param.type.name,
        "default": param.default,
        "is_flag": param.is_flag,
        "count": param.count,
        "help": param.help,
    }


@pytest.mark.parametrize("name", _SHARED + ("reprocess",))
def test_the_commands_share_each_option(name):
    seen = [_option(c, name) for c in _COMMANDS.values()]
    if name == "reprocess":
        for option in seen:
            option.pop("help")
    assert all(option == seen[0] for option in seen), seen
