"""Operator-visible cadence outcomes for update-mainline."""

import pytest
from click.testing import CliRunner

from mimir.cli.mainline import update_mainline_command


def _skips(caplog):
    return [
        (r.levelname, r.getMessage())
        for r in caplog.records
        if r.name == "mimir.cli.mainline"
    ]


@pytest.mark.parametrize("verbose", [False, True])
def test_recent_tree_skip_logs_at_info(monkeypatch, caplog, verbose):
    from mimir.broker import client

    class Client:
        def call(self, request):
            assert request.force is False
            return {
                "trees": {
                    "linus": {
                        "skipped": True,
                        "last_walked_seconds_ago": 312,
                        "walk_interval_seconds": 1500,
                    }
                }
            }

    monkeypatch.setattr(client, "get_broker_client", Client)
    result = CliRunner().invoke(update_mainline_command, ["-v"] if verbose else [])
    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert _skips(caplog) == (
        [("INFO", "update-mainline: linus skipped (walked 312s ago, interval 1500s)")]
        if verbose
        else []
    )


def test_old_skipped_payload_and_failure_precedence(monkeypatch, caplog):
    from mimir.broker import client

    class Client:
        def call(self, request):
            return {
                "trees": {
                    "linus": {"skipped": True},
                    "next": {"skipped": True, "ok": False, "error": "fetch failed"},
                }
            }

    monkeypatch.setattr(client, "get_broker_client", Client)
    result = CliRunner().invoke(update_mainline_command, ["-v"])
    assert result.exit_code == 1
    assert result.output.splitlines() == [
        "update-mainline: tree next failed: fetch failed",
    ]
    assert _skips(caplog) == [
        ("INFO", "update-mainline: linus skipped (walk interval not elapsed)")
    ]
