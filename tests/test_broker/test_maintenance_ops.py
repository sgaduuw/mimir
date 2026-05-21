"""End-to-end tests for the Phase 2.3 maintenance-op family:
`update_mainline`, `analyze`, `vacuum`.

Each op is exercised twice: directly via the broker client (to pin
the protocol + handler wiring) and via the click command (to pin
the CLI dispatcher's broker shim). Together they catch a regression
in either the RPC plumbing or the CLI delegation."""

import os

import pytest
from click.testing import CliRunner
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

from mimir.broker import handlers
from mimir.broker.client import BrokerClient
from mimir.cli.maintenance import analyze_command, vacuum_command
from mimir.config import settings
from tests.test_broker._helpers import broker_running, short_socket_path


# ----- classify_op --------------------------------------------------------


def test_classify_op_recognises_phase_2_3_ops():
    """The three Phase 2.3 ops must route to the long queue, not
    the cache queue. Pinning the membership here catches a future
    accidental omission in `handlers.LONG_OPS`."""
    for op in ("update_mainline", "analyze", "vacuum"):
        assert op in handlers.LONG_OPS


# ----- analyze ------------------------------------------------------------


def test_analyze_via_broker_runs(seeded_db):
    """End-to-end: broker handles the analyze RPC and returns an
    elapsed_ms count. The seeded DB is tiny so the ANALYZE
    completes in single-digit ms; we just verify the shape."""
    sp = short_socket_path("analyze-rpc")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            result = c.analyze()
        finally:
            c.close()
    assert isinstance(result, dict)
    assert result.get("full") is False
    assert "elapsed_ms" in result
    assert isinstance(result["elapsed_ms"], int)


def test_analyze_full_via_broker_runs(seeded_db):
    """`full=True` propagates through the RPC and the handler runs
    the no-cap pass. We can't easily distinguish the two passes
    from outside (both produce a `Reply.ok=True`); the reply's
    `full` field is the structural signal."""
    sp = short_socket_path("analyze-full-rpc")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            result = c.analyze(full=True)
        finally:
            c.close()
    assert result.get("full") is True


def test_analyze_cli_dispatches_via_broker(seeded_db, monkeypatch):
    """`mimir analyze` checks `settings.broker_socket_path` and
    routes via the broker client when set. Without the dispatch
    shim, the CLI would run a second direct-write ANALYZE outside
    the single-writer broker, defeating the migration."""
    sp = short_socket_path("analyze-cli")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        # `get_broker_client` is a process singleton; clear it so
        # this test's client points at our tmp socket, not whatever
        # a prior test set up.
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            result = CliRunner().invoke(analyze_command, [])
        finally:
            _client_mod.reset_broker_client()
    assert result.exit_code == 0, result.output
    assert "ANALYZE complete" in result.output


# ----- vacuum -------------------------------------------------------------


def test_vacuum_via_broker_runs(seeded_db):
    """End-to-end: broker handles the vacuum RPC and returns the
    `VacuumResult` dict. Small DB so reclaimed is likely 0; we
    verify the shape, not the savings."""
    sp = short_socket_path("vacuum-rpc")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            result = c.vacuum()
        finally:
            c.close()
    assert "elapsed_ms" in result
    assert "db_size_before" in result
    assert "db_size_after" in result
    assert "reclaimed" in result


def test_vacuum_logs_warning_at_start(seeded_db, caplog):
    """The handler logs a high-visibility WARNING so an operator
    correlating cache-write stalls against the broker log can tell
    "weekly maintenance, not a fault." Pinning the line catches a
    regression that silently drops the warning."""
    import logging

    caplog.set_level(logging.WARNING, logger="mimir.broker.handlers.maintenance")
    sp = short_socket_path("vacuum-warn")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            c.vacuum()
        finally:
            c.close()
    msgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "mimir.broker.handlers.maintenance" in r.name
    ]
    assert any("pausing for VACUUM" in m for m in msgs), (
        f"expected vacuum WARNING; saw {msgs}"
    )


def test_vacuum_cli_dispatches_via_broker(seeded_db, monkeypatch):
    """`mimir vacuum` routes via the broker when configured."""
    sp = short_socket_path("vacuum-cli")
    with broker_running(sp):
        monkeypatch.setattr(settings, "broker_socket_path", sp)
        from mimir.broker import client as _client_mod

        _client_mod.reset_broker_client()
        try:
            result = CliRunner().invoke(vacuum_command, [])
        finally:
            _client_mod.reset_broker_client()
    assert result.exit_code == 0, result.output
    assert "reclaimed" in result.output


# ----- update_mainline ----------------------------------------------------


def _build_minimal_tree_with_maintainers(repo_path) -> str:
    """Make a bare repo at `repo_path` containing a HEAD commit
    whose tree has a `MAINTAINERS` blob with one section. Returns
    the head sha. Mirrors the pattern used by `test_cli_maintainers`
    but produces a real entry rather than the missing-blob case."""
    os.makedirs(repo_path)
    repo = Repo.init_bare(str(repo_path))
    maintainers_text = (
        b"\n"
        b"FAKE SUBSYSTEM\n"
        b"M:\tFake Maintainer <fake@example.org>\n"
        b"S:\tMaintained\n"
        b"F:\tdrivers/fake/\n"
    )
    blob = Blob.from_string(maintainers_text)
    repo.object_store.add_object(blob)
    tree = Tree()
    tree.add(b"MAINTAINERS", 0o100644, blob.id)
    repo.object_store.add_object(tree)
    commit = Commit()
    commit.tree = tree.id
    commit.parents = []
    commit.author = commit.committer = b"t <t@x>"
    commit.commit_time = commit.author_time = 1700000000
    commit.commit_timezone = commit.author_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = b"seed"
    repo.object_store.add_object(commit)
    repo.refs[b"HEAD"] = commit.id
    return commit.id.decode("ascii")


def test_update_mainline_via_broker_loads_maintainers(seeded_db, tmp_path, monkeypatch):
    """End-to-end: broker handles update_mainline RPC, reads
    MAINTAINERS from the fake tree, and reports the subsystem
    count. We pass `skip_fetch=True, skip_commits=True` to scope
    the test to the MAINTAINERS phase only (the Link-trailer walk
    has its own coverage in test_mainline)."""
    repo_path = tmp_path / "linux.git"
    head_sha = _build_minimal_tree_with_maintainers(repo_path)
    monkeypatch.setattr(settings, "mainline_tree_path", str(repo_path))

    sp = short_socket_path("update-mainline-rpc")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            result = c.update_mainline(skip_fetch=True, skip_commits=True)
        finally:
            c.close()

    assert result["maintainers_ran"] is True
    assert result["subsystems_loaded"] == 1
    assert result["mainline_head"] == head_sha
    # Skipped phase: commits_ran must be False so a future regression
    # that forgets to honor the skip flag surfaces here.
    assert result["commits_ran"] is False


def test_update_mainline_via_broker_reports_unchanged_on_second_call(
    seeded_db, tmp_path, monkeypatch
):
    """Second call against the same HEAD: MAINTAINERS short-circuits
    and reports `maintainers_unchanged=True, subsystems_loaded=0`.
    Same shape the steady-state scheduler tick produces."""
    repo_path = tmp_path / "linux.git"
    _build_minimal_tree_with_maintainers(repo_path)
    monkeypatch.setattr(settings, "mainline_tree_path", str(repo_path))

    sp = short_socket_path("update-mainline-unchanged")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            c.update_mainline(skip_fetch=True, skip_commits=True)
            second = c.update_mainline(skip_fetch=True, skip_commits=True)
        finally:
            c.close()

    assert second["maintainers_unchanged"] is True
    assert second["subsystems_loaded"] == 0


def test_update_mainline_via_broker_force_reloads(seeded_db, tmp_path, monkeypatch):
    """`force=True` overrides the unchanged-HEAD short-circuit, so
    the second call reloads even though HEAD hasn't moved."""
    repo_path = tmp_path / "linux.git"
    _build_minimal_tree_with_maintainers(repo_path)
    monkeypatch.setattr(settings, "mainline_tree_path", str(repo_path))

    sp = short_socket_path("update-mainline-force")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            c.update_mainline(skip_fetch=True, skip_commits=True)
            second = c.update_mainline(
                skip_fetch=True,
                skip_commits=True,
                force=True,
            )
        finally:
            c.close()

    assert second["maintainers_ran"] is True
    assert second["subsystems_loaded"] == 1


# ----- interleaved long + cache (regression for two-worker design) --------


def test_cache_op_completes_while_maintenance_op_runs(seeded_db, tmp_path, monkeypatch):
    """Pin the two-worker invariant: a cache.set RPC must complete
    while a long-running update_mainline RPC is in flight on the
    long worker. Otherwise the cache worker has stalled."""
    import threading
    import time

    from mimir import cache
    from mimir.broker.client import BrokerClient

    repo_path = tmp_path / "linux.git"
    _build_minimal_tree_with_maintainers(repo_path)
    monkeypatch.setattr(settings, "mainline_tree_path", str(repo_path))

    sp = short_socket_path("interleave")
    with broker_running(sp):
        c_long = BrokerClient(sp)
        c_cache = BrokerClient(sp)
        try:
            done = threading.Event()

            def _long_op():
                try:
                    c_long.update_mainline(
                        skip_fetch=True,
                        skip_commits=True,
                        force=True,
                    )
                finally:
                    done.set()

            t = threading.Thread(target=_long_op, daemon=True)
            t.start()
            # Give the long op a moment to actually start.
            time.sleep(0.05)
            # Cache op on a parallel client must succeed without
            # waiting for the long op to finish.
            c_cache.cache_set(cache._ns("interleave-key"), '"v"', 60)
            t.join(timeout=10)
            assert done.is_set(), "long op didn't complete in time"
        finally:
            c_long.close()
            c_cache.close()

    assert cache.get("interleave-key") == "v"


@pytest.mark.parametrize("op_name", ["update_mainline", "analyze", "vacuum"])
def test_dispatch_table_routes_phase_2_3_ops(op_name):
    """The dispatch table in `handlers/__init__._DISPATCH` carries
    each new op. Without an entry, the broker would return
    `UnknownOp` rather than running the handler."""
    from mimir.broker.handlers import _DISPATCH

    assert op_name in _DISPATCH
    model, handler = _DISPATCH[op_name]
    # Sanity check: the handler is callable and the model is a
    # pydantic class (not None or a placeholder).
    assert callable(handler)
    assert hasattr(model, "model_validate")
