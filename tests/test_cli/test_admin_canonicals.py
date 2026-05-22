"""Tests for mimir/cli/admin/canonicals.py: the
`admin canonicals backfill` command surface."""

from click.testing import CliRunner

from mimir.cli import (
    admin_canonicals_backfill_command,
)


def test_admin_canonicals_backfill_runs_and_prints_summary(seeded_db):
    result = CliRunner().invoke(admin_canonicals_backfill_command, [])
    assert result.exit_code == 0, result.output
    # Seeded DB has 4 articles, but none with list-shaped To/Cc -- so
    # examined>0, resolved=0, unresolved>0. We only pin the line
    # shape; counts depend on dashboard helpers and bootstrap state.
    assert "backfill complete:" in result.output
    assert "examined=" in result.output
    assert "resolved=" in result.output


# `admin inbox add / update / remove / show` -- service layer is
# covered by test_inboxes.py; here we pin the click wiring (option
# parsing, ClickException -> exit-nonzero, output shape).
