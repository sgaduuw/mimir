"""Warm diagnostics reach the broker's production stream handler."""

import logging
import queue
import time

import pytest
from click.testing import CliRunner

from mimir.broker.client import BrokerClient
from mimir.broker.protocol import (
    Reply,
    WarmGlobalRequest,
    WarmInboxRequest,
    WarmSubsystemRequest,
)
from mimir.broker.server import _BrokerServer, _ReplyDelivery
from mimir.config import settings
from mimir.models import Subsystem
from tests.test_broker._helpers import broker_running, short_socket_path


def _broker_output(monkeypatch, serve):
    from mimir.cli import broker

    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    monkeypatch.setattr(broker, "serve", serve)
    result = CliRunner().invoke(
        broker.broker_command, ["--socket", str(short_socket_path("warm-log"))]
    )
    assert result.exit_code == 0, result.output
    return result.output.splitlines()


@pytest.mark.parametrize("op", ["warm_inbox", "warm_subsystem", "warm_global"])
@pytest.mark.parametrize("threshold", [1, 0])
@pytest.mark.parametrize("workers", [1, 4])
def test_slow_warm_has_one_info_diagnostic_at_real_sink(
    seeded_db, monkeypatch, op, threshold, workers
):
    monkeypatch.setattr(settings, "broker_warm_workers", workers)
    output = _warm_output(seeded_db, monkeypatch, op, threshold, fail=False)
    timings = [
        line
        for line in output
        if "broker warm slow" in line or "broker slow rpc" in line
    ]
    assert len(timings) == (1 if threshold > 0 else 0), output
    if threshold > 0:
        assert f" INFO broker warm slow [{op}] " in timings[0]


@pytest.mark.parametrize("op", ["warm_inbox", "warm_subsystem", "warm_global"])
@pytest.mark.parametrize("threshold", [1, 100000])
def test_warm_target_failure_still_warns_at_real_sink(
    seeded_db, monkeypatch, op, threshold
):
    output = _warm_output(seeded_db, monkeypatch, op, threshold, fail=True)
    errors = [line for line in output if "broker warm:" in line and "failed:" in line]
    assert len(errors) == 1, output
    assert " WARNING " in errors[0]
    assert "injected target failure" in errors[0]
    assert not any("broker slow rpc" in line for line in output), output


def _warm_output(seeded_db, monkeypatch, op, threshold, *, fail):
    from mimir.cli import cache

    with seeded_db() as session:
        sub = Subsystem(name="LOGTEST", status="Maintained")
        session.add(sub)
        session.commit()
        sub_id = sub.id
    calls = []

    def target(session):
        calls.append(1)
        time.sleep(0.02)
        if fail:
            raise RuntimeError("injected target failure")
        return ["probe"]

    monkeypatch.setattr(cache, "_build_inbox_targets", lambda *a: [("probe", target)])
    monkeypatch.setattr(cache, "_build_global_targets", lambda *a: [("probe", target)])
    monkeypatch.setattr(
        cache, "_per_subsystem_warm_call", lambda session, *a: target(session)
    )
    monkeypatch.setattr(settings, "broker_slow_rpc_warn_ms", threshold)
    request = {
        "warm_inbox": WarmInboxRequest(inbox_name="alpha"),
        "warm_subsystem": WarmSubsystemRequest(inbox_name="alpha", subsystem_id=sub_id),
        "warm_global": WarmGlobalRequest(),
    }[op]

    def serve(path):
        with broker_running(path):
            client = BrokerClient(path)
            try:
                result = client.call(request)
                assert bool(result["errors"]) is fail
            finally:
                client.close()

    output = _broker_output(monkeypatch, serve)
    assert calls == [1]
    return output


@pytest.mark.parametrize(
    "worker,ok,outcome",
    [
        ("warm", False, "sent"),
        ("warm", True, "failed"),
        ("cache", True, "sent"),
        ("long", True, "sent"),
    ],
)
def test_other_slow_reply_warnings_reach_real_sink(monkeypatch, worker, ok, outcome):
    monkeypatch.setattr(settings, "broker_slow_rpc_warn_ms", 1)

    def serve(path):
        delivery = _ReplyDelivery(
            line=b'{"op":"probe"}',
            conn=None,
            queue=queue.Queue(),
            worker_tag=worker,
            queue_wait_ms=20,
            started=time.perf_counter(),
        )
        server = object.__new__(_BrokerServer)
        server.warm_queue = delivery.queue if worker == "warm" else queue.Queue()
        server._log_reply(
            Reply(rpc_id=1, ok=ok, error=None if ok else "UnknownInbox"),
            delivery,
            outcome,
        )

    output = _broker_output(monkeypatch, serve)
    warnings = [line for line in output if "broker slow rpc" in line]
    assert len(warnings) == 1, output
    assert f" WARNING broker slow rpc [{worker}] " in warnings[0]
    assert f"ok={ok}, delivery={outcome}" in warnings[0]
