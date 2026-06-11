"""End-to-end integration: pipelined RPCs against a real broker.

Validates the full path: client allocates rpc_ids, sends concurrent
requests, broker dispatches across multiple worker threads, each
worker writes a reply under the per-connection send_lock, client
demux thread resolves futures by rpc_id."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time


from mimir import cache as cache_mod
from mimir.broker.client import BrokerClient
from tests.test_broker._helpers import broker_running, short_socket_path

# Project root needed for alembic and for launching the broker CLI as
# a subprocess. Both require cwd=PROJECT_ROOT so alembic.ini resolves
# and mimir is importable.
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def test_sixteen_concurrent_pings_all_complete(seeded_db, monkeypatch):
    """Sixteen caller threads each issue a ping; all complete
    successfully. Broker has at least one cache worker (default).
    Pipelining means the workers' replies all land back at the
    client demux thread and resolve to the correct futures."""
    # Bump cache workers so the broker can actually drain 16 pings
    # in parallel rather than serializing them at one worker.
    from mimir.config import settings

    monkeypatch.setattr(settings, "broker_cache_workers", 4)

    sp = short_socket_path("integration-16-pings")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            results = [None] * 16

            def fire(i: int) -> None:
                results[i] = c.ping()

            threads = [threading.Thread(target=fire, args=(i,)) for i in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
            for i, t in enumerate(threads):
                assert not t.is_alive(), f"thread {i} hung"
            assert all(results), results
        finally:
            c.close()


def test_mixed_op_types_concurrent(seeded_db, monkeypatch):
    """Mix cache_set, ping across threads. Every reply demuxes to
    the correct caller. Verifies all 8 cache writes landed."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "broker_cache_workers", 4)

    sp = short_socket_path("integration-mixed")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:

            def do_set(i: int) -> None:
                c.cache_set(cache_mod._ns(f"key_{i}"), f'"v_{i}"', 60)

            def do_ping(slot: list) -> None:
                slot.append(c.ping())

            ping_slots: list = []
            threads = []
            for i in range(8):
                threads.append(threading.Thread(target=do_set, args=(i,)))
                threads.append(threading.Thread(target=do_ping, args=(ping_slots,)))

            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
            for i, t in enumerate(threads):
                assert not t.is_alive(), f"thread {i} hung"
            # All 8 pings should have returned True.
            assert len(ping_slots) == 8
            assert all(ping_slots), ping_slots
        finally:
            c.close()

    # Verify all eight cache writes landed (post-broker; read direct).
    for i in range(8):
        assert cache_mod.get(f"key_{i}") == f"v_{i}"


def test_broker_subprocess_with_tracemalloc_enabled(tmp_path):
    """Full broker subprocess, TRACEMALLOC_INTERVAL_SECONDS=1, sends a
    few RPCs, terminates cleanly, and snapshot-failed errors do not
    appear in stderr.

    The broker's default diagnostics_dir is /data/diagnostics, which is
    typically not writable in the test environment. The snapshotter's
    mkdir-fail path triggers instead: it logs an ERROR "snapshotter
    disabled" and returns without spawning a thread. This is intentional:
    we exercise the graceful-degradation path under the failure branch
    while confirming the broker still serves and shuts down cleanly.

    The "actually wrote a snapshot file" assertion lives in the unit
    tests (tests/test_broker/test_tracemalloc.py) where tmp_path is
    passed as diagnostics_dir directly.

    Pinned by _claude/specs/2026-06-11-broker-tracemalloc-diagnostic-design.md.
    """
    # macOS default tmp_path is under /private/var/folders/... which
    # exceeds the AF_UNIX 104-byte limit. Route via /tmp with a short
    # name, same pattern the _helpers module uses for short_socket_path.
    socket_path = "/tmp" + f"/mimir-tracemalloc-{os.getpid()}.sock"
    db_path = tmp_path / "mimir.db"

    env = {
        **os.environ,
        "TRACEMALLOC_INTERVAL_SECONDS": "1",
        "DATABASE_URL": f"sqlite:///{db_path}",
        "SECRET_KEY": "x" * 32,
        "MIMIR_IS_BROKER": "true",
        # Keep broker_socket_path pointing at a non-existent default so
        # the broker doesn't try to RPC to itself; it will only listen on
        # the socket we pass via --socket.
    }

    # Schema bootstrap: alembic upgrade head against the isolated DB
    # before starting the broker (the broker expects the schema to exist
    # before accepting RPCs).
    alembic_result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert alembic_result.returncode == 0, (
        f"alembic upgrade failed:\n{alembic_result.stderr}"
    )

    # Launch the broker as a subprocess using the mimir console-script.
    # mimir.cli is a package (no __main__.py), so `-m mimir.cli` does not
    # work. The `mimir` console script lives alongside the Python
    # executable inside the virtualenv.
    mimir_script = os.path.join(os.path.dirname(sys.executable), "mimir")
    proc = subprocess.Popen(
        [mimir_script, "broker", "--socket", socket_path],
        cwd=_PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait up to 10 s for the broker's socket to appear. The broker
        # calls bind() + listen() before serve_forever() blocks, so the
        # socket file is created before the first accept() attempt.
        deadline = time.monotonic() + 10.0
        while not os.path.exists(socket_path):
            if time.monotonic() > deadline:
                break
            time.sleep(0.05)
        assert os.path.exists(socket_path), (
            "broker socket did not appear within 10 s; "
            f"stderr so far: {proc.stderr.read1(4096).decode(errors='replace')!r}"
        )

        # Send a few ping RPCs via raw JSONL (mirrors the broker-ping CLI).
        # Each ping uses a fresh connection; that is fine for this test.
        for rpc_id in range(3):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect(socket_path)
            request = json.dumps({"rpc_id": rpc_id, "op": "ping"}).encode() + b"\n"
            sock.sendall(request)
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                assert chunk, "broker closed connection before sending a reply"
                buf += chunk
            sock.close()
            reply = json.loads(buf.split(b"\n")[0])
            assert reply.get("ok") is True, f"ping {rpc_id} returned {reply!r}"

        # Wait long enough for at least one snapshotter interval to elapse
        # (TRACEMALLOC_INTERVAL_SECONDS=1). In the mkdir-fail path no thread
        # is spawned, so this wait is just a liveness check rather than a
        # file-landing check.
        time.sleep(2.0)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        # Clean up the socket file if the broker didn't remove it on exit.
        if os.path.exists(socket_path):
            os.unlink(socket_path)

    # Clean shutdown: 0 (normal Python exit) or -SIGTERM (terminated by signal).
    assert proc.returncode in (0, -signal.SIGTERM), (
        f"broker exited with unexpected code {proc.returncode}"
    )

    stderr = proc.stderr.read().decode(errors="replace")
    # This substring appears when the snapshotter's periodic write loop
    # raises an unexpected exception AFTER startup. It must not appear;
    # any failure should be the graceful "snapshotter disabled" startup
    # path, not a mid-run crash.
    assert "tracemalloc snapshot failed" not in stderr, (
        f"unexpected snapshot-failure error in broker stderr:\n{stderr}"
    )
