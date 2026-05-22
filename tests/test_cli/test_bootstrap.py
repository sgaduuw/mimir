"""`mimir bootstrap-inboxes` CLI: direct path + broker dispatch.

Phase 2.0 (1.35.0) routes this CLI through the broker when
`BROKER_SOCKET_PATH` is set. Tests pin both paths so a future
regression in either surfaces in CI.
"""

from click.testing import CliRunner

from mimir.cli import bootstrap_inboxes_command


def test_bootstrap_inboxes_direct_path_works(seeded_db):
    """With broker mode off (default in tests), the CLI calls
    `mimir.inboxes.bootstrap_inboxes()` directly and echoes the
    count. Smoke test for the pre-Phase-2 path that's still in the
    code as the fallback."""
    result = CliRunner().invoke(bootstrap_inboxes_command)
    assert result.exit_code == 0
    assert "inbox(es) reconciled" in result.output
    # No "(via broker)" suffix in direct mode.
    assert "via broker" not in result.output


def test_bootstrap_inboxes_via_broker(seeded_db, monkeypatch):
    """With `BROKER_SOCKET_PATH` set, the CLI sends the
    `bootstrap_inboxes` RPC to the broker. End-to-end smoke: spin a
    broker on a tmp socket, point settings at it, invoke the CLI.
    Confirms broker dispatch and the "(via broker)" suffix the
    operator-visible echo uses to distinguish the path."""
    from mimir.broker.client import reset_broker_client
    from mimir.config import settings
    from tests.test_broker._helpers import broker_running, short_socket_path

    sp = short_socket_path("cli-bootstrap")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        reset_broker_client()
        try:
            result = CliRunner().invoke(bootstrap_inboxes_command)
        finally:
            reset_broker_client()

    assert result.exit_code == 0, result.output
    assert "inbox(es) reconciled (via broker)" in result.output


def test_bootstrap_inboxes_broker_unavailable_fails_cleanly(
    seeded_db,
    monkeypatch,
):
    """When `BROKER_SOCKET_PATH` is set but no broker is listening,
    the CLI exits non-zero with a clear error message. Hard-fail
    rather than silently falling back to the direct path, since
    `bootstrap-inboxes` is a scheduler startup step and silently
    succeeding via the direct path would mask the misconfiguration.
    """
    from mimir.broker.client import reset_broker_client
    from mimir.config import settings
    from tests.test_broker._helpers import short_socket_path

    # Point at a path with no broker behind it.
    sp = short_socket_path("cli-no-broker")
    monkeypatch.setattr(settings, "broker_socket_path", sp)
    reset_broker_client()
    try:
        result = CliRunner().invoke(bootstrap_inboxes_command)
    finally:
        reset_broker_client()

    assert result.exit_code != 0
    # ClickException prints to stderr; CliRunner merges by default.
    assert "broker bootstrap_inboxes failed" in result.output
