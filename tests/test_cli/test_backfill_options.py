"""The backfill commands share one option set (`backfill_options`).
Two pins so an edit to one command cannot drift from the rest: the shared
options are identical across commands, and each command's `--help` is
byte-for-byte the text operators already know."""

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from mimir.cli.admin.canonicals import admin_canonicals_backfill_command
from mimir.cli.backfill import (
    backfill_article_files_command,
    backfill_article_trailers_command,
    backfill_patch_series_command,
    backfill_series_chains_command,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "cli_help"

_COMMANDS = {
    "backfill-article-files": backfill_article_files_command,
    "backfill-article-trailers": backfill_article_trailers_command,
    "backfill-patch-series": backfill_patch_series_command,
    "backfill-series-chains": backfill_series_chains_command,
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


@pytest.mark.parametrize("name", _COMMANDS)
def test_help_output_is_unchanged(name):
    # Byte identity depends on click's help layout; a click upgrade that
    # reflows it fails here, and the fixtures are then regenerated.
    result = CliRunner().invoke(_COMMANDS[name], ["--help"], terminal_width=80)
    assert result.exit_code == 0, result.output
    assert result.output == (_FIXTURES / f"{name}.txt").read_text()
