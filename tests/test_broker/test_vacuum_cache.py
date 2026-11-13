"""VACUUM rejects best-effort cache RPCs without occupying the writer."""

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from mimir.broker.handlers import cache as handlers
from mimir.broker.handlers.maintenance import handle_vacuum
from mimir.broker.protocol import (
    CacheSetRequest,
    VacuumRequest,
)
from mimir.maintenance import VacuumResult

MUTATIONS = [
    (handlers.handle_cache_set, CacheSetRequest(key="test", value_json='"x"', ttl=60)),
]


def _result():
    return VacuumResult(
        elapsed_ms=1, db_size_before=100, db_size_after=100, reclaimed=0
    )


@pytest.mark.parametrize("handler,req", MUTATIONS)
def test_cache_mutations_fail_fast_during_vacuum(monkeypatch, handler, req):
    entered, release = threading.Event(), threading.Event()

    def vacuum():
        entered.set()
        assert release.wait(15), "test did not release VACUUM"
        return _result()

    monkeypatch.setattr("mimir.maintenance.run_vacuum", vacuum)
    writer = Mock()
    writer.submit.return_value = Future()
    monkeypatch.setattr(handlers, "get_active_writer", lambda: writer)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(handle_vacuum, VacuumRequest())
        try:
            assert entered.wait(5), "VACUUM never started"
            start = time.monotonic()
            reply = handler(req.model_copy(update={"rpc_id": 17}))
            elapsed = time.monotonic() - start
            assert (reply.ok, reply.error, reply.rpc_id) == (False, "vacuum", 17)
            assert elapsed < 0.5
            writer.submit.assert_not_called()
        finally:
            release.set()
            running.result(timeout=5)


@pytest.mark.parametrize("first_fails", [False, True])
def test_overlapping_vacuums_keep_gate_closed_and_count_exactly(
    monkeypatch, caplog, first_fails
):
    import logging

    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = iter(range(2))
    lock = threading.Lock()

    def vacuum():
        with lock:
            i = next(calls)
        entered[i].set()
        assert release[i].wait(10), "test did not release VACUUM"
        if i == 0 and first_fails:
            raise RuntimeError("vacuum failed")
        return _result()

    monkeypatch.setattr("mimir.maintenance.run_vacuum", vacuum)
    writer = Mock()
    completed = Future()
    completed.set_result(3)
    writer.submit.return_value = completed
    monkeypatch.setattr(handlers, "get_active_writer", lambda: writer)
    caplog.set_level(logging.INFO, logger="mimir.broker.handlers.maintenance")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(handle_vacuum, VacuumRequest())
        second = None
        try:
            assert entered[0].wait(3)
            second = pool.submit(handle_vacuum, VacuumRequest())
            assert entered[1].wait(3), "both VACUUM handlers must overlap"
            with ThreadPoolExecutor(max_workers=8) as clients:
                replies = list(
                    clients.map(
                        lambda _: handlers.handle_cache_set(MUTATIONS[0][1]), range(80)
                    )
                )
            assert len(replies) == 80
            assert all(not r.ok and r.error == "vacuum" for r in replies)
            release[0].set()
            if first_fails:
                with pytest.raises(RuntimeError, match="vacuum failed"):
                    first.result(timeout=3)
            else:
                assert first.result(timeout=3).ok
            assert not second.done(), "the second VACUUM must still be running"
            assert handlers.handle_cache_set(MUTATIONS[0][1]).error == "vacuum"
            writer.submit.assert_not_called()
            assert not any(
                r.getMessage().startswith("broker: skipped ") for r in caplog.records
            )
        finally:
            release[0].set()
            release[1].set()
            if second is not None:
                second.result(timeout=3)
    summaries = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("broker: skipped ")
    ]
    assert summaries == ["broker: skipped 81 cache writes during VACUUM"]
    for handler, req in MUTATIONS:
        reply = handler(req)
        assert reply.ok
    assert writer.submit.call_count == 1
    # A new maintenance window gets a fresh counter, including after failure.
    monkeypatch.setattr("mimir.maintenance.run_vacuum", _result)
    handle_vacuum(VacuumRequest())
    assert (
        caplog.records[-2].getMessage()
        == "broker: skipped 0 cache writes during VACUUM"
    )


def test_failed_vacuum_reopens_cache_admission(monkeypatch):
    def vacuum():
        assert handlers.handle_cache_set(MUTATIONS[0][1]).error == "vacuum"
        raise RuntimeError("vacuum failed")

    monkeypatch.setattr("mimir.maintenance.run_vacuum", vacuum)
    with pytest.raises(RuntimeError, match="vacuum failed"):
        handle_vacuum(VacuumRequest())
    writer = Mock()
    done = Future()
    done.set_result(None)
    writer.submit.return_value = done
    monkeypatch.setattr(handlers, "get_active_writer", lambda: writer)
    assert handlers.handle_cache_set(MUTATIONS[0][1]).ok
    writer.submit.assert_called_once()


@pytest.mark.parametrize("handler,req", MUTATIONS)
@pytest.mark.parametrize("error", ["vacuum", "writer busy"])
def test_cache_callers_quiet_only_expected_vacuum_rejections(
    monkeypatch, caplog, handler, req, error
):
    from pathlib import Path

    from mimir import cache
    from mimir.broker.client import BrokerClient
    from mimir.broker.protocol import Reply

    client = BrokerClient(Path("/unused"))
    monkeypatch.setattr(
        client, "_rpc", lambda req, timeout=None: Reply(rpc_id=1, ok=False, error=error)
    )
    monkeypatch.setattr("mimir.broker.client.get_broker_client", lambda: client)
    monkeypatch.setattr(cache, "_should_dispatch_to_broker", lambda: True)
    calls = {
        "cache_set": lambda: cache.set("test", "x", ttl=60),
    }
    calls[req.op]()
    warnings = [r for r in caplog.records if r.name == "mimir.cache"]
    assert len(warnings) == (0 if error == "vacuum" else 1)
    if warnings:
        assert error in warnings[0].getMessage()


@pytest.mark.parametrize("req", [pair[1] for pair in MUTATIONS])
def test_client_keeps_cache_skip_exception_contract(monkeypatch, req):
    from pathlib import Path

    from mimir.broker.client import (
        BrokerCacheWriteSkipped,
        BrokerClient,
        BrokerUnavailable,
    )
    from mimir.broker.protocol import PingRequest, Reply

    client = BrokerClient(Path("/unused"))
    monkeypatch.setattr(
        client,
        "_rpc",
        lambda req, timeout=None: Reply(rpc_id=1, ok=False, error="vacuum"),
    )
    with pytest.raises(BrokerCacheWriteSkipped) as exc:
        client.call(req)
    assert isinstance(exc.value, BrokerUnavailable)
    assert str(exc.value) == f"{req.op}: vacuum"
    with pytest.raises(BrokerUnavailable) as exc:
        client.call(PingRequest())
    assert type(exc.value) is BrokerUnavailable


def test_skip_summary_reaches_configured_broker_logging_sink():
    import re
    import subprocess
    import sys

    script = """
from mimir.cli._common import _configure_logging
from mimir.broker.handlers.cache import handle_cache_set
from mimir.broker.handlers.maintenance import handle_vacuum
from mimir.broker.protocol import CacheSetRequest, VacuumRequest
from mimir.maintenance import VacuumResult
import mimir.maintenance

def vacuum():
    for i in range(3):
        reply = handle_cache_set(CacheSetRequest(key="test", value_json='"x"', ttl=60))
        assert reply.error == "vacuum"
    return VacuumResult(elapsed_ms=1, db_size_before=1, db_size_after=1, reclaimed=0)

_configure_logging(1)
mimir.maintenance.run_vacuum = vacuum
handle_vacuum(VacuumRequest())
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    counts = re.findall(
        r"broker: skipped (\d+) cache writes during VACUUM", result.stderr
    )
    assert [int(n) for n in counts] == [3]
    assert "writer busy" not in result.stderr
