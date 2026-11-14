"""Operator-visible cadence outcomes for update-mainline."""

from click.testing import CliRunner

from mimir.cli.mainline import update_mainline_command


def test_recent_tree_is_visible_without_verbose(monkeypatch):
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
    result = CliRunner().invoke(update_mainline_command, [])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "update-mainline: linus skipped (walked 312s ago, interval 1500s)"
    ]


def test_old_skipped_payload_and_failure_precedence(monkeypatch):
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
    result = CliRunner().invoke(update_mainline_command, [])
    assert result.exit_code == 1
    assert result.output.splitlines() == [
        "update-mainline: linus skipped (walk interval not elapsed)",
        "update-mainline: tree next failed: fetch failed",
    ]
