"""CLI broker-unavailable failure-mode coverage.

The 2.0.0 single-writer invariant (CONTEXT.md "Single-writer
invariant via the broker (v2.0.0+)") drops every direct-write
fallback from the CLI commands that mutate the DB. Each affected
command catches `BrokerUnavailable` from `get_broker_client()` and
re-raises as `click.ClickException(...)` so a misconfigured deploy
(broker container not running, wrong `BROKER_SOCKET_PATH`, the
broker container crashed but the scheduler is still trying to
dispatch) surfaces as a clean exit-non-zero with a clear message
rather than a silent no-op or an opaque traceback.

`tests/test_cli/test_bootstrap.py:test_bootstrap_inboxes_broker_unavailable_fails_cleanly`
covers this for `bootstrap-inboxes` (the canonical example). This
file extends the same shape to every other CLI command that
dispatches through the broker, parametrised so the dispatch
contract is pinned in one place.

A regression that silently swallowed `BrokerUnavailable` (e.g. a
refactor that wrapped it in a try/except without re-raising, or
that introduced a silent direct-write fallback path) would only
surface today on test_bootstrap.py; this file catches the broader
class.
"""

import pytest
from click.testing import CliRunner

from mimir.cli import (
    admin_canonicals_backfill_command,
    admin_failures_replay_command,
    admin_inbox_add_command,
    analyze_command,
    backfill_article_files_command,
    backfill_article_trailers_command,
    backfill_patch_series_command,
    ingest_command,
    update_mainline_command,
    vacuum_command,
    warm_cache_command,
)


@pytest.fixture
def unreachable_broker(monkeypatch):
    """Point `settings.broker_socket_path` at a path with no broker
    listening, and reset the broker-client process-singleton so the
    next `get_broker_client()` connects fresh against the dud path.

    Yields the path so individual tests can reference it in their
    assertions if needed.
    """
    from mimir.broker.client import reset_broker_client
    from mimir.config import settings
    from tests.test_broker._helpers import short_socket_path

    sp = short_socket_path("cli-broker-unavailable")
    monkeypatch.setattr(settings, "broker_socket_path", sp)
    reset_broker_client()
    try:
        yield sp
    finally:
        reset_broker_client()


# The parametrise rows. Each row pins ONE dispatch site; the test
# body is identical across them.
#
# Tuple is `(label, click_command, args)`. `label` controls the
# pytest-node ID so failures are immediately readable. `args` is the
# CLI argv passed to `CliRunner().invoke(...)`; empty for commands
# whose required surface is options-only (everything has defaults).
# `admin inbox add` carries one positional NAME argument because the
# Click parser fails-fast on missing positionals BEFORE reaching the
# broker-dispatch try/except, which would mask the contract.
_BROKER_DISPATCH_COMMANDS = [
    pytest.param(ingest_command, [], id="ingest"),
    pytest.param(update_mainline_command, [], id="update-mainline"),
    pytest.param(warm_cache_command, [], id="warm-cache"),
    pytest.param(backfill_article_files_command, [], id="backfill-article-files"),
    pytest.param(backfill_article_trailers_command, [], id="backfill-article-trailers"),
    pytest.param(backfill_patch_series_command, [], id="backfill-patch-series"),
    pytest.param(analyze_command, [], id="analyze"),
    pytest.param(vacuum_command, [], id="vacuum"),
    pytest.param(admin_canonicals_backfill_command, [], id="admin-canonicals-backfill"),
    # `admin failures replay` takes INBOX_NAME as a positional;
    # pass a string so Click's parser advances to the broker
    # dispatch rather than rejecting argv first.
    pytest.param(
        admin_failures_replay_command,
        ["test-fail-replay-inbox"],
        id="admin-failures-replay",
    ),
    pytest.param(admin_inbox_add_command, ["test-fail-add"], id="admin-inbox-add"),
]


@pytest.mark.parametrize("command,args", _BROKER_DISPATCH_COMMANDS)
def test_cli_command_exits_non_zero_when_broker_unavailable(
    command, args, unreachable_broker
):
    """Pin the broker-unavailable failure shape across every CLI
    command that dispatches through the broker.

    Each command must:
    1. Exit non-zero (a misconfigured deploy must NOT silently
       succeed).
    2. Surface an error message that names the broker (so the
       operator knows where to look).

    The 2.0.0 broker is mandatory; there is no direct-write
    fallback. A regression that silently routed around
    `BrokerUnavailable` (returning ok or no-op'ing) would let the
    misconfigured deploy stay broken until a downstream symptom
    (stale cache, missing migrations, drifted mainline) eventually
    surfaced. Catching it at CLI exit is the contract.

    test_bootstrap.py already pins this for `bootstrap-inboxes`;
    this test extends to the broader command set.
    """
    result = CliRunner().invoke(command, args)

    assert result.exit_code != 0, (
        f"command {command.name!r} returned exit_code=0 despite the "
        f"broker socket pointing at a non-existent path. Output:\n"
        f"{result.output}\n"
        f"Did the command silently fall back to a direct-write path "
        f"(removed in 2.0.0) or swallow BrokerUnavailable?"
    )

    # The error message must reference the broker so an operator
    # can diagnose. ClickException prints to stderr; CliRunner
    # captures both by default into `result.output`.
    assert "broker" in result.output.lower(), (
        f"command {command.name!r} exited non-zero but the error "
        f"message doesn't mention 'broker'. An operator hitting this "
        f"in production needs the message to name the failing "
        f"dispatcher.\n\nOutput:\n{result.output}"
    )
    assert "failed" in result.output.lower(), (
        f"command {command.name!r} error message must include 'failed' "
        f"so the line is grep-able alongside other CLI failures.\n\n"
        f"Output:\n{result.output}"
    )
