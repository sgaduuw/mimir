"""Tests for `mimir tracemalloc-diff` CLI command.

See _claude/specs/2026-06-11-broker-tracemalloc-diagnostic-design.md.
"""

from click.testing import CliRunner

from mimir.cli.diagnostics import tracemalloc_diff_command


def test_diff_command_corrupt_input_errors_cleanly(tmp_path):
    """A non-pickle file passed as snapshot input fails with a
    Click BadParameter (exit code != 0) and a useful message."""
    bogus = tmp_path / "not-a-pickle.txt"
    bogus.write_text("this is not a pickle")
    other = tmp_path / "other.txt"
    other.write_text("also not a pickle")
    runner = CliRunner()
    result = runner.invoke(tracemalloc_diff_command, [str(bogus), str(other)])
    assert result.exit_code != 0
    assert "not-a-pickle.txt" in result.output or "not-a-pickle.txt" in str(
        result.exception
    )
