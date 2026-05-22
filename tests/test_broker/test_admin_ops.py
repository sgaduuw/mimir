"""End-to-end tests for the Phase 2.4 admin-op family: the seven
`inbox_*` RPCs and `failures_replay`.

Each RPC is exercised in two layers: directly via the broker
client (pins the wire protocol + handler + service-layer
roundtrip) and via the click CLI command (pins the CLI dispatch
shim translates broker errors back into operator-facing
ClickException text identically to the direct path)."""

import pytest
from click.testing import CliRunner

from mimir.broker import handlers
from mimir.broker.client import BrokerClient, BrokerUnavailable
from mimir.cli.admin.inbox import (
    admin_inbox_add_command,
    admin_inbox_remove_command,
    admin_inbox_trackers_add_command,
    admin_inbox_trackers_clear_command,
    admin_inbox_trackers_remove_command,
    admin_inbox_trackers_set_command,
    admin_inbox_update_command,
)
from mimir.cli.admin.robots import (
    admin_robots_add_command,
    admin_robots_remove_command,
    admin_robots_reset_command,
    admin_robots_update_command,
)
from mimir.config import settings
from tests.test_broker._helpers import broker_running, short_socket_path


# ----- classify_op --------------------------------------------------------


def test_classify_op_recognises_phase_2_4_ops():
    """The 8 Phase 2.4 ops must route to the long queue."""
    for op in (
        "failures_replay",
        "inbox_create",
        "inbox_update",
        "inbox_delete",
        "inbox_set_tracked_authors",
        "inbox_add_tracked_author",
        "inbox_remove_tracked_author",
        "inbox_clear_tracked_authors",
    ):
        assert op in handlers.LONG_OPS


@pytest.mark.parametrize(
    "op_name",
    [
        "failures_replay",
        "inbox_create",
        "inbox_update",
        "inbox_delete",
        "inbox_set_tracked_authors",
        "inbox_add_tracked_author",
        "inbox_remove_tracked_author",
        "inbox_clear_tracked_authors",
    ],
)
def test_dispatch_table_routes_phase_2_4_ops(op_name):
    """The dispatch table carries each new op. Without an entry,
    the broker would return `UnknownOp` rather than running the
    handler."""
    from mimir.broker.handlers import _DISPATCH

    assert op_name in _DISPATCH
    model, handler = _DISPATCH[op_name]
    assert callable(handler)
    assert hasattr(model, "model_validate")


# ----- inbox_create -------------------------------------------------------


def test_inbox_create_via_broker_inserts_row(seeded_db):
    """End-to-end: broker handler creates a new Inbox row and
    returns the projected dict. The new inbox is visible to the
    next read."""
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Inbox

    sp = short_socket_path("inbox-create")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            payload = c.inbox_create(
                "delta",
                mirror_path="Inboxes/delta/git",
                upstream_url="https://lore.kernel.org/delta",
            )
        finally:
            c.close()

    assert payload["name"] == "delta"
    assert payload["mirror_path"] == "Inboxes/delta/git"
    assert payload["upstream_url"] == "https://lore.kernel.org/delta"

    with SessionLocal() as s:
        row = s.execute(
            select(Inbox).where(Inbox.name == "delta"),
        ).scalar_one()
    assert row.mirror_path == "Inboxes/delta/git"


def test_inbox_create_duplicate_returns_invalid_error(seeded_db):
    """Creating a second inbox with the same name raises
    InboxValidationError server-side; the client surfaces it as a
    BrokerUnavailable whose message carries the `InvalidInbox:`
    prefix."""
    sp = short_socket_path("inbox-create-dup")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            with pytest.raises(BrokerUnavailable) as ei:
                # alpha already exists in the seeded fixture.
                c.inbox_create(
                    "alpha",
                    mirror_path="Inboxes/alpha/git",
                    upstream_url="https://lore.kernel.org/alpha",
                )
        finally:
            c.close()
    assert "InvalidInbox:" in str(ei.value)


# ----- inbox_update -------------------------------------------------------


def test_inbox_update_via_broker_mutates_only_supplied_fields(seeded_db):
    """Only the request's non-None fields land server-side. Pinning
    that contract here avoids a regression where an absent field
    silently writes NULL."""
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Inbox

    sp = short_socket_path("inbox-update")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            payload = c.inbox_update(
                "alpha",
                upstream_url="https://lore.kernel.org/alpha-new",
            )
        finally:
            c.close()

    assert payload["upstream_url"] == "https://lore.kernel.org/alpha-new"
    # mirror_path was NOT in the request; must be untouched.
    with SessionLocal() as s:
        row = s.execute(
            select(Inbox).where(Inbox.name == "alpha"),
        ).scalar_one()
    assert row.upstream_url == "https://lore.kernel.org/alpha-new"
    assert row.mirror_path  # whatever the seeded value was, non-empty


def test_inbox_update_unknown_returns_not_found(seeded_db):
    sp = short_socket_path("inbox-update-404")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            with pytest.raises(BrokerUnavailable) as ei:
                c.inbox_update("nosuch", upstream_url="https://x.example/")
        finally:
            c.close()
    assert "InboxNotFound:" in str(ei.value)


# ----- inbox_delete -------------------------------------------------------


def test_inbox_delete_via_broker_removes_row(seeded_db):
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Inbox

    sp = short_socket_path("inbox-delete")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            # First create something we can delete (alpha has linked
            # articles; deleting it cascades. Use a fresh inbox to
            # keep the test scoped.)
            c.inbox_create(
                "to-delete",
                mirror_path="Inboxes/to-delete/git",
                upstream_url="https://lore.kernel.org/to-delete",
            )
            report = c.inbox_delete("to-delete")
        finally:
            c.close()

    assert report["name"] == "to-delete"
    with SessionLocal() as s:
        gone = s.execute(
            select(Inbox).where(Inbox.name == "to-delete"),
        ).scalar_one_or_none()
    assert gone is None


# ----- tracker mutators ---------------------------------------------------


def test_inbox_set_tracked_authors_via_broker(seeded_db):
    sp = short_socket_path("trackers-set")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            payload = c.inbox_set_tracked_authors(
                "alpha",
                {"linus": "torvalds@", "greg": "gregkh@"},
            )
        finally:
            c.close()
    assert payload["tracked_authors"] == {
        "linus": "torvalds@",
        "greg": "gregkh@",
    }


def test_inbox_add_tracked_author_via_broker(seeded_db):
    sp = short_socket_path("trackers-add")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            c.inbox_set_tracked_authors("alpha", {})
            payload = c.inbox_add_tracked_author("alpha", "linus", "torvalds@")
        finally:
            c.close()
    assert payload["tracked_authors"] == {"linus": "torvalds@"}


def test_inbox_remove_tracked_author_via_broker(seeded_db):
    sp = short_socket_path("trackers-rm")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            c.inbox_set_tracked_authors(
                "alpha",
                {"linus": "torvalds@", "greg": "gregkh@"},
            )
            payload = c.inbox_remove_tracked_author("alpha", "linus")
        finally:
            c.close()
    assert payload["tracked_authors"] == {"greg": "gregkh@"}


def test_inbox_clear_tracked_authors_via_broker(seeded_db):
    sp = short_socket_path("trackers-clear")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            c.inbox_set_tracked_authors(
                "alpha",
                {"linus": "torvalds@"},
            )
            payload = c.inbox_clear_tracked_authors("alpha")
        finally:
            c.close()
    assert not payload["tracked_authors"]


# ----- failures_replay ----------------------------------------------------


def test_failures_replay_via_broker_unknown_inbox(seeded_db):
    """The handler resolves `inbox_name` server-side; an unknown
    name surfaces as InboxNotFound."""
    sp = short_socket_path("failures-replay-404")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            with pytest.raises(BrokerUnavailable) as ei:
                c.failures_replay("nosuch")
        finally:
            c.close()
    assert "InboxNotFound:" in str(ei.value)


def test_failures_replay_via_broker_empty_inbox_returns_zero_counters(seeded_db):
    """`alpha` has no parse_failures in the seeded fixture. The
    handler returns all-zero counters without touching the DB."""
    sp = short_socket_path("failures-replay-empty")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            result = c.failures_replay("alpha")
        finally:
            c.close()
    assert result == {
        "attempted": 0,
        "recovered": 0,
        "still_failed": 0,
        "skipped": 0,
    }


# ----- CLI dispatch shims -------------------------------------------------


def test_cli_admin_inbox_add_dispatches_via_broker(seeded_db, monkeypatch):
    """`mimir admin inbox add` routes through the broker when
    BROKER_SOCKET_PATH is set. Without the dispatch shim, the CLI
    would do a direct INSERT outside the single-writer broker."""
    sp = short_socket_path("cli-inbox-add")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            result = CliRunner().invoke(
                admin_inbox_add_command,
                ["epsilon"],
            )
        finally:
            _client_mod.reset_broker_client()
    assert result.exit_code == 0, result.output
    assert "created inbox 'epsilon'" in result.output


def test_cli_admin_inbox_add_preserves_validation_error_text(seeded_db, monkeypatch):
    """A duplicate-name error reaches the operator as the bare
    text the direct path would emit (e.g. "inbox 'alpha' already
    exists"), not as "broker inbox_create failed: ...". The
    `_inbox_click_error` helper unwraps the structured error."""
    sp = short_socket_path("cli-inbox-dup")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            result = CliRunner().invoke(
                admin_inbox_add_command,
                ["alpha"],  # already exists
            )
        finally:
            _client_mod.reset_broker_client()
    assert result.exit_code != 0
    assert "already exists" in result.output
    assert "broker" not in result.output


def test_cli_admin_inbox_update_dispatches_via_broker(seeded_db, monkeypatch):
    sp = short_socket_path("cli-inbox-update")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            result = CliRunner().invoke(
                admin_inbox_update_command,
                ["alpha", "--upstream-url", "https://x.example/alpha"],
            )
        finally:
            _client_mod.reset_broker_client()
    assert result.exit_code == 0, result.output
    assert "updated inbox 'alpha'" in result.output


def test_cli_admin_inbox_remove_dispatches_via_broker(seeded_db, monkeypatch):
    sp = short_socket_path("cli-inbox-remove")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            # Create something to remove so we don't disturb seeded
            # fixtures.
            create = CliRunner().invoke(
                admin_inbox_add_command,
                ["throwaway"],
            )
            assert create.exit_code == 0, create.output

            result = CliRunner().invoke(
                admin_inbox_remove_command,
                ["throwaway", "--yes"],
            )
        finally:
            _client_mod.reset_broker_client()
    assert result.exit_code == 0, result.output
    assert "removed inbox 'throwaway'" in result.output


def test_cli_admin_inbox_trackers_set_dispatches_via_broker(seeded_db, monkeypatch):
    sp = short_socket_path("cli-trackers-set")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            result = CliRunner().invoke(
                admin_inbox_trackers_set_command,
                ["alpha", "linus=torvalds@"],
            )
        finally:
            _client_mod.reset_broker_client()
    assert result.exit_code == 0, result.output
    assert "alpha: set 1 tracker" in result.output


def test_cli_admin_inbox_trackers_add_dispatches_via_broker(seeded_db, monkeypatch):
    sp = short_socket_path("cli-trackers-add")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            CliRunner().invoke(
                admin_inbox_trackers_set_command,
                ["alpha"],
                catch_exceptions=True,
            )
            result = CliRunner().invoke(
                admin_inbox_trackers_add_command,
                ["alpha", "greg", "gregkh@"],
            )
        finally:
            _client_mod.reset_broker_client()
    # set with no pairs is a no-op for our purposes; the add
    # command's outcome is what we pin.
    assert result.exit_code == 0, result.output
    assert "added tracker 'greg'" in result.output


def test_cli_admin_inbox_trackers_remove_dispatches_via_broker(
    seeded_db,
    monkeypatch,
):
    sp = short_socket_path("cli-trackers-rm")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            CliRunner().invoke(
                admin_inbox_trackers_set_command,
                ["alpha", "linus=torvalds@"],
            )
            result = CliRunner().invoke(
                admin_inbox_trackers_remove_command,
                ["alpha", "linus"],
            )
        finally:
            _client_mod.reset_broker_client()
    assert result.exit_code == 0, result.output
    assert "removed tracker 'linus'" in result.output


def test_cli_admin_inbox_trackers_clear_dispatches_via_broker(
    seeded_db,
    monkeypatch,
):
    sp = short_socket_path("cli-trackers-clear")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            CliRunner().invoke(
                admin_inbox_trackers_set_command,
                ["alpha", "linus=torvalds@"],
            )
            result = CliRunner().invoke(
                admin_inbox_trackers_clear_command,
                ["alpha"],
            )
        finally:
            _client_mod.reset_broker_client()
    assert result.exit_code == 0, result.output
    assert "alpha: cleared all trackers" in result.output


# ----- broker self-bootstrap ANALYZE -------------------------------------


def test_broker_self_bootstrap_analyze_runs_when_sentinel_missing(seeded_db, caplog):
    """On startup the broker checks for `.broker_initial_analyze`
    in the socket's parent dir; if missing, runs a bounded ANALYZE
    and touches the sentinel. Subsequent restarts skip the pass."""
    import logging

    caplog.set_level(logging.INFO, logger="mimir.broker.server")
    sp = short_socket_path("self-bootstrap-analyze")
    sentinel = sp.parent / ".broker_initial_analyze"
    # Force a clean state.
    if sentinel.exists():
        sentinel.unlink()

    try:
        with broker_running(sp):
            pass
        assert sentinel.exists(), (
            "broker should have touched the post-migrate ANALYZE sentinel"
        )
        msgs = [r.getMessage() for r in caplog.records]
        assert any("running post-migrate ANALYZE" in m for m in msgs), (
            f"expected post-migrate ANALYZE log; saw {msgs}"
        )
    finally:
        if sentinel.exists():
            sentinel.unlink()


def test_broker_self_bootstrap_analyze_skipped_when_sentinel_present(
    seeded_db,
    caplog,
):
    """If the sentinel exists at startup, the broker skips the
    inline ANALYZE pass and emits a DEBUG line instead of the INFO
    one."""
    import logging

    caplog.set_level(logging.DEBUG, logger="mimir.broker.server")
    sp = short_socket_path("self-bootstrap-skip")
    sentinel = sp.parent / ".broker_initial_analyze"
    sentinel.touch()
    try:
        with broker_running(sp):
            pass
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("running post-migrate ANALYZE" in m for m in msgs), (
            f"sentinel present; broker should have skipped ANALYZE; saw {msgs}"
        )
        assert any("sentinel" in m and "skipping" in m for m in msgs), (
            f"expected sentinel-present skip log; saw {msgs}"
        )
    finally:
        if sentinel.exists():
            sentinel.unlink()


# ----- robots admin ------------------------------------------------------


def test_classify_op_recognises_robots_ops():
    """The four robots admin ops must route to the long queue."""
    for op in ("robots_add", "robots_update", "robots_remove", "robots_reset"):
        assert op in handlers.LONG_OPS


@pytest.mark.parametrize(
    "op_name",
    ["robots_add", "robots_update", "robots_remove", "robots_reset"],
)
def test_dispatch_table_routes_robots_ops(op_name):
    from mimir.broker.handlers import _DISPATCH

    assert op_name in _DISPATCH
    model, handler = _DISPATCH[op_name]
    assert callable(handler)
    assert hasattr(model, "model_validate")


def test_robots_add_via_broker_inserts_row(seeded_db):
    from mimir.extensions import SessionLocal

    sp = short_socket_path("robots-add")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            payload = c.robots_add("GPTBot", disallow=["/"], crawl_delay=10)
        finally:
            c.close()

    assert payload == {
        "user_agent": "GPTBot",
        "crawl_delay": 10,
        "disallow_paths": ["/"],
    }

    from mimir import robots as svc

    with SessionLocal() as s:
        rule = svc.get_rule(s, "GPTBot")
    assert rule is not None
    assert rule.disallow_paths == ["/"]


def test_robots_add_duplicate_returns_invalid_error(seeded_db):
    """Adding a UA that already has a row surfaces as
    InvalidRobotsRule on the wire; client raises BrokerUnavailable
    carrying the structured prefix."""
    from mimir import robots as svc

    svc.add_rule("GPTBot", disallow=["/"])

    sp = short_socket_path("robots-add-dup")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            with pytest.raises(BrokerUnavailable) as ei:
                c.robots_add("GPTBot", disallow=["/"])
        finally:
            c.close()
    assert "InvalidRobotsRule:" in str(ei.value)


def test_robots_update_via_broker_mutates_row(seeded_db):
    sp = short_socket_path("robots-update")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            payload = c.robots_update(
                "*",
                add_disallow=["/private/"],
                crawl_delay=30,
            )
        finally:
            c.close()
    assert payload["user_agent"] == "*"
    assert payload["crawl_delay"] == 30
    assert "/private/" in payload["disallow_paths"]
    assert "/*/attachment/" in payload["disallow_paths"]


def test_robots_remove_via_broker_drops_row(seeded_db):
    from mimir import robots as svc
    from mimir.extensions import SessionLocal

    svc.add_rule("GPTBot", disallow=["/"])

    sp = short_socket_path("robots-remove")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            c.robots_remove("GPTBot")
        finally:
            c.close()

    with SessionLocal() as s:
        assert svc.get_rule(s, "GPTBot") is None


def test_robots_remove_star_refused(seeded_db):
    """Service-layer guard fires server-side; client raises with the
    `InvalidRobotsRule:` prefix."""
    from mimir.extensions import SessionLocal

    sp = short_socket_path("robots-remove-star")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            with pytest.raises(BrokerUnavailable) as ei:
                c.robots_remove("*")
        finally:
            c.close()
    assert "InvalidRobotsRule:" in str(ei.value)
    from mimir import robots as svc

    with SessionLocal() as s:
        assert svc.get_rule(s, "*") is not None


def test_robots_reset_via_broker_reseeds_defaults(seeded_db):
    from mimir import robots as svc
    from mimir.extensions import SessionLocal

    svc.add_rule("GPTBot", disallow=["/"])
    svc.update_rule("*", crawl_delay=99)

    sp = short_socket_path("robots-reset")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            c.robots_reset()
        finally:
            c.close()

    with SessionLocal() as s:
        rules = svc.list_rules(s)
    assert [r.user_agent for r in rules] == ["*"]
    assert rules[0].crawl_delay == 5
    assert rules[0].disallow_paths == ["/*/attachment/"]


def test_cli_admin_robots_add_dispatches_via_broker(seeded_db, monkeypatch):
    """`mimir admin robots add` routes through the broker when
    BROKER_SOCKET_PATH is set."""
    from mimir.extensions import SessionLocal

    sp = short_socket_path("cli-robots-add")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            result = CliRunner().invoke(
                admin_robots_add_command,
                ["GPTBot", "--disallow", "/"],
            )
        finally:
            _client_mod.reset_broker_client()
    assert result.exit_code == 0, result.output
    assert "added rule for user_agent 'GPTBot'" in result.output

    from mimir import robots as svc

    with SessionLocal() as s:
        assert svc.get_rule(s, "GPTBot") is not None


def test_cli_admin_robots_remove_star_preserves_error_text(seeded_db, monkeypatch):
    """`admin robots remove '*'` should surface the bare service-layer
    error text via the CLI, not the broker wrapper text."""
    sp = short_socket_path("cli-robots-rm-star")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            result = CliRunner().invoke(
                admin_robots_remove_command,
                ["*"],
            )
        finally:
            _client_mod.reset_broker_client()
    assert result.exit_code != 0
    assert "cannot remove the '*' stanza" in result.output
    assert "broker" not in result.output


def test_cli_admin_robots_update_dispatches_via_broker(seeded_db, monkeypatch):
    sp = short_socket_path("cli-robots-update")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            result = CliRunner().invoke(
                admin_robots_update_command,
                ["*", "--add-disallow", "/private/"],
            )
        finally:
            _client_mod.reset_broker_client()
    assert result.exit_code == 0, result.output
    assert "/private/" in result.output


def test_cli_admin_robots_reset_dispatches_via_broker(seeded_db, monkeypatch):
    sp = short_socket_path("cli-robots-reset")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            result = CliRunner().invoke(
                admin_robots_reset_command,
                ["--yes"],
            )
        finally:
            _client_mod.reset_broker_client()
    assert result.exit_code == 0, result.output
    assert "reset" in result.output
