"""#1123: a broker job that hangs must be diagnosable and reported.

Three surfaces: the client's timeout error names the request, the
broker WARNs (repeatedly) about a job still running past its client
timeout, and SIGUSR1 dumps every thread's stack without stopping the
broker."""

import logging
import os
import re
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from mimir.broker import handlers
from mimir.broker import watchdog as watchdog_mod
from mimir.broker.client import BrokerClient, BrokerUnavailable
from mimir.broker.protocol import (
    CacheSetRequest,
    Reply,
    WarmSubsystemRequest,
    describe_request,
)
from mimir.config import settings
from tests.test_broker._helpers import broker_running, short_socket_path


def _field(text: str, name: str) -> str | None:
    m = re.search(rf"(?:^|\s){name}=(\S+)", text)
    return m.group(1) if m else None


def _stuck_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "broker stuck job" in r.getMessage()
    ]


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


@pytest.fixture
def blocking_warm_subsystem(monkeypatch):
    """Replace the warm_subsystem handler with one that blocks until
    released, the stand-in for the workers that hung on 2026-11-08.
    Yields `(started, release)` events."""
    started = threading.Event()
    release = threading.Event()

    def blocked(req):
        started.set()
        release.wait(timeout=30)
        return Reply(rpc_id=req.rpc_id, ok=True)

    monkeypatch.setitem(
        handlers._DISPATCH, "warm_subsystem", (WarmSubsystemRequest, blocked)
    )
    try:
        yield started, release
    finally:
        release.set()


def test_describe_request_names_the_target_and_skips_the_payload():
    req = CacheSetRequest(key="k" * 400, value_json='"' + "v" * 1000 + '"', ttl=5)
    req.rpc_id = 9
    text = describe_request(req.model_dump())

    assert text.split()[0] == "cache_set"
    assert _field(text, "rpc_id") == "9"
    key = _field(text, "key")
    assert key is not None and key.endswith("...") and len(key) == 120
    assert "value_json" not in text and "vvvv" not in text
    assert _field(text, "ttl") is None


def test_client_timeout_names_the_op_and_its_target(
    seeded_db, monkeypatch, blocking_warm_subsystem
):
    """The scheduler's only trace of the 2026-11-08 hang was
    `rpc timed out after 300.0s` with no hint of which of its RPCs
    hung."""
    monkeypatch.setattr(WarmSubsystemRequest, "timeout", 0.2)
    sp = short_socket_path("wd-client-timeout")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            with pytest.raises(BrokerUnavailable) as exc_info:
                c.call(WarmSubsystemRequest(inbox_name="lkml", subsystem_id=42))
        finally:
            c.close()
    text = str(exc_info.value)
    assert text.startswith("rpc timed out after 0.2s: warm_subsystem ")
    assert _field(text, "inbox_name") == "lkml"
    assert _field(text, "subsystem_id") == "42"
    assert _field(text, "rpc_id") is not None


def test_job_past_its_client_timeout_is_warned_about_and_repeated(
    seeded_db, caplog, blocking_warm_subsystem
):
    """Steps the watchdog's clock rather than sleeping through the real
    300 s warm timeout. The threshold is the op's own client timeout,
    and the WARNING repeats at the interval while the job still runs."""
    started, release = blocking_warm_subsystem
    caplog.set_level(logging.WARNING, logger="mimir.broker.watchdog")
    sp = short_socket_path("wd-repeat")
    with broker_running(sp) as server:
        c = BrokerClient(sp)
        try:
            with pytest.raises(BrokerUnavailable):
                c.call(
                    WarmSubsystemRequest(inbox_name="lkml", subsystem_id=42),
                    timeout=0.1,
                )
            assert started.wait(5)
            # Precondition: the hung job is registered as in flight.
            jobs = server.in_flight
            with jobs._lock:
                (thread_name,) = jobs._jobs
                t0 = jobs._jobs[thread_name].started
            limit = WarmSubsystemRequest.timeout

            assert jobs.check(60, now=t0 + limit - 1) == 0
            assert jobs.check(60, now=t0 + limit + 1) == 1
            assert jobs.check(60, now=t0 + limit + 30) == 0  # inside interval
            assert jobs.check(60, now=t0 + limit + 62) == 1  # repeated

            msgs = _stuck_warnings(caplog)
            assert len(msgs) == 2
            for msg in msgs:
                assert "[warm-" in msg or "[warm]" in msg
                assert thread_name in msg and thread_name.startswith("broker-warm")
                assert " warm_subsystem " in msg
                assert _field(msg, "inbox_name") == "lkml"
                assert _field(msg, "subsystem_id") == "42"
            elapsed = [float(re.search(r"running (\d+)s", m).group(1)) for m in msgs]
            assert elapsed == [limit + 1, limit + 62]

            release.set()
            _wait_for(lambda: not jobs._jobs)
            assert jobs.check(60, now=t0 + 10 * limit) == 0
        finally:
            c.close()


def test_watchdog_thread_reports_a_running_job_repeatedly(
    seeded_db, monkeypatch, caplog, blocking_warm_subsystem
):
    """End to end through the real watchdog thread `start_workers`
    starts: no clock stepping, small thresholds instead."""
    started, release = blocking_warm_subsystem
    monkeypatch.setattr(watchdog_mod, "POLL_SEC", 0.02)
    monkeypatch.setattr(WarmSubsystemRequest, "timeout", 0.1)
    monkeypatch.setattr(settings, "broker_stuck_job_warn_interval_sec", 0.2)
    caplog.set_level(logging.WARNING, logger="mimir.broker.watchdog")
    sp = short_socket_path("wd-thread")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            with pytest.raises(BrokerUnavailable):
                c.call(WarmSubsystemRequest(inbox_name="lkml", subsystem_id=7))
            assert started.wait(5)
            _wait_for(lambda: len(_stuck_warnings(caplog)) >= 3)
        finally:
            release.set()
            c.close()
    for msg in _stuck_warnings(caplog):
        assert _field(msg, "subsystem_id") == "7"


def test_fast_jobs_are_never_reported(seeded_db, caplog):
    """The common case: a job finishes well inside its timeout and
    leaves nothing behind for the watchdog."""
    from mimir.broker.protocol import PingRequest

    caplog.set_level(logging.WARNING, logger="mimir.broker.watchdog")
    sp = short_socket_path("wd-fast")
    with broker_running(sp) as server:
        c = BrokerClient(sp)
        try:
            for _ in range(5):
                c.call(PingRequest())
        finally:
            c.close()
        _wait_for(lambda: not server.in_flight._jobs)
        assert server.in_flight.check(1, now=time.monotonic() + 1e6) == 0
    assert _stuck_warnings(caplog) == []


def test_sigusr1_dumps_every_thread_and_the_process_keeps_running():
    """Run in a child process: the signal is process-wide, and the
    dump goes to the real stderr file descriptor."""
    script = textwrap.dedent(
        """
        import os, signal, threading, time
        from mimir.broker.server import _register_stack_dump_signal

        _register_stack_dump_signal()
        gate = threading.Event()

        def hung_warm_worker_here():
            gate.wait(30)

        t = threading.Thread(target=hung_warm_worker_here, daemon=True)
        t.start()
        time.sleep(0.2)
        os.kill(os.getpid(), signal.SIGUSR1)
        time.sleep(0.2)
        print("still running", flush=True)
        gate.set()
        t.join()
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "still running"
    # Both threads appear: the blocked one by its frame, and the main one.
    headers = re.findall(
        r"^(?:Current thread|Thread) 0x[0-9a-f]+", proc.stderr, re.MULTILINE
    )
    assert len(headers) >= 2
    assert "hung_warm_worker_here" in proc.stderr


def test_sigusr1_is_registered_before_the_server_is_built(monkeypatch, tmp_path):
    """build_server runs migrations and backfills that can take minutes.
    A SIGUSR1 sent then must dump stacks, not kill the broker."""
    import faulthandler
    import signal

    from mimir.broker import server as server_mod

    seen = []

    def build_server(_socket_path):
        # unregister() returns True only if a handler was registered.
        seen.append(faulthandler.unregister(signal.SIGUSR1))
        raise RuntimeError("stop after the check")

    monkeypatch.setattr(server_mod, "build_server", build_server)
    with pytest.raises(RuntimeError, match="stop after the check"):
        server_mod.serve(tmp_path / "broker.sock")
    assert seen == [True]
