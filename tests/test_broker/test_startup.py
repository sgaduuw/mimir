"""Database startup ordering and its readiness boundary."""

import logging
from types import SimpleNamespace

import pytest

from mimir.broker import server, startup


@pytest.mark.parametrize("schema_changed", [False, True])
def test_prepare_database_preserves_order_and_starts_verification_without_joining(
    monkeypatch, tmp_path, schema_changed
):
    calls = []
    socket_path = tmp_path / "broker.sock"

    def migrate(path):
        assert path == socket_path
        calls.append("migrate")
        return schema_changed

    def step(name):
        def run(path):
            assert path == socket_path
            calls.append(name)

        return run

    def analyze(path, changed):
        assert path == socket_path
        assert changed is schema_changed
        calls.append("analyze")

    def verify():
        calls.append("verify")
        # A readiness-blocking join is an error, even if it would finish quickly.
        return SimpleNamespace(join=lambda **kwargs: pytest.fail("joined verifier"))

    monkeypatch.setattr(startup, "_migrate_if_needed", migrate)
    monkeypatch.setattr(startup, "_bootstrap_inboxes_if_needed", step("bootstrap"))
    monkeypatch.setattr(startup, "_post_migrate_analyze_if_needed", analyze)
    monkeypatch.setattr(startup, "_backfill_thread_roots_if_needed", step("backfill"))
    monkeypatch.setattr(startup, "_verify_thread_roots_async", verify)

    startup.prepare_database(socket_path)

    assert calls == ["migrate", "bootstrap", "analyze", "backfill", "verify"]


@pytest.mark.parametrize("preparation_fails", [False, True])
def test_build_server_prepares_database_before_listening(
    monkeypatch, tmp_path, caplog, preparation_fails
):
    socket_path = tmp_path / "broker.sock"
    calls = []
    broker = SimpleNamespace(start_workers=lambda: calls.append("workers"))
    monkeypatch.setattr(server, "_BrokerServer", lambda path: broker)
    monkeypatch.setattr(server.os, "chmod", lambda path, mode: None)

    def prepare(path):
        assert path == socket_path
        assert calls == ["workers"]
        assert not any(r.msg == "broker: listening on %s" for r in caplog.records)
        calls.append("prepare")
        if preparation_fails:
            raise RuntimeError("database preparation failed")

    monkeypatch.setattr(startup, "prepare_database", prepare)
    with caplog.at_level(logging.INFO, logger="mimir.broker.server"):
        if preparation_fails:
            with pytest.raises(RuntimeError, match="database preparation failed"):
                server.build_server(socket_path)
        else:
            assert server.build_server(socket_path) is broker

    assert calls == ["workers", "prepare"]
    listening = [r for r in caplog.records if r.msg == "broker: listening on %s"]
    assert len(listening) == (0 if preparation_fails else 1)
