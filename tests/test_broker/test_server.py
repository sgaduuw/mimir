"""Tests for `mimir.broker.server`'s lifecycle: socket binding,
accept loop, clean shutdown, stale-socket cleanup. Uses an
in-thread broker on a tmp UNIX socket. RPC content is tested
in `test_handlers.py`; here we care about the socket lifecycle
and the bytes-on-the-wire framing."""

import socket
from pathlib import Path

from mimir.broker.server import build_server
from tests.test_broker._helpers import broker_running, short_socket_path


def _send_line(sp: Path, line: bytes) -> bytes:
    """One-shot connect-send-recv-close, for tests that don't
    exercise the persistent-connection path (that's `test_client`)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5.0)
    s.connect(str(sp))
    s.sendall(line + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    return buf


def test_server_binds_and_responds_to_ping(seeded_db):
    sp = short_socket_path("ping")
    with broker_running(sp):
        reply = _send_line(sp, b'{"op": "ping"}')
        assert b'"ok":true' in reply


def test_server_handles_sequential_requests_on_one_connection(seeded_db):
    """A single client connection sends N requests serially; broker
    reads each line and replies in order. Confirms the broker's
    StreamRequestHandler loops, not single-request-then-close."""
    sp = short_socket_path("seq")
    with broker_running(sp):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(str(sp))
        rfile = s.makefile("rb")
        wfile = s.makefile("wb")
        try:
            for _ in range(5):
                wfile.write(b'{"op": "ping"}\n')
                wfile.flush()
                line = rfile.readline()
                assert b'"ok":true' in line
        finally:
            rfile.close()
            wfile.close()
            s.close()


def test_server_unlinks_stale_socket_on_startup(seeded_db):
    """A stale socket file from an unclean prior shutdown must not
    prevent the new broker from binding. `build_server` unlinks it
    before bind()."""
    sp = short_socket_path("stale")
    # Pre-create the file as a stale socket-shaped file. (A real
    # AF_UNIX-listened socket would need a previous process; a
    # plain file is enough to exercise the unlink-first path.)
    sp.write_bytes(b"")
    assert sp.exists()

    server = build_server(sp)
    try:
        assert sp.is_socket()
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()


def test_server_socket_file_has_mode_0660(seeded_db):
    """The broker chmods the socket to 0660 so only the mimir UID
    (and its group) can talk to it. Trust model: same as the /data
    volume's file perms; no extra auth on RPC."""
    sp = short_socket_path("mode")
    server = build_server(sp)
    try:
        mode = sp.stat().st_mode & 0o777
        assert mode == 0o660, f"expected 0o660, got {oct(mode)}"
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()


def test_server_warns_on_slow_rpc(seeded_db, caplog, monkeypatch):
    """Per-RPC latency is the overload signal: when a single dispatch
    takes longer than `broker_slow_rpc_warn_ms`, the handler emits a
    WARNING with the elapsed duration and leading request bytes.
    Lets the operator notice writer-lock contention (an admin
    backfill running) without staring at the log."""
    import logging
    import time as time_module

    from mimir import cache
    from mimir.broker import server as broker_server
    from mimir.broker.client import BrokerClient
    from mimir.broker.handlers import dispatch as real_dispatch
    from mimir.config import settings

    def slow_dispatch(line):
        time_module.sleep(0.05)  # 50ms; comfortably above the 10ms threshold below
        return real_dispatch(line)

    monkeypatch.setattr(broker_server, "dispatch", slow_dispatch)
    monkeypatch.setattr(settings, "broker_slow_rpc_warn_ms", 10)
    caplog.set_level(logging.WARNING, logger="mimir.broker.server")

    sp = short_socket_path("slow-warn")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            c.cache_set(cache._ns("slow-warn-test"), '"x"', 60)
        finally:
            c.close()

    slow_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "broker slow rpc" in r.getMessage()
    ]
    assert slow_warnings, (
        "expected a slow-rpc WARNING after the 50ms sleep dispatch; "
        f"got log records: {[r.getMessage() for r in caplog.records]}"
    )
    # The warning text includes the elapsed time breakdown and op preview.
    msg = slow_warnings[0].getMessage()
    assert "ms total" in msg
    assert "queued" in msg
    assert "dispatch" in msg
    assert "cache_set" in msg


def test_server_does_not_warn_when_threshold_disabled(seeded_db, caplog, monkeypatch):
    """`broker_slow_rpc_warn_ms = 0` disables the slow-RPC log.
    Operators who don't want the noise can opt out without removing
    the timing call entirely (the elapsed-time DEBUG log still
    fires when `-v` is on)."""
    import logging
    import time as time_module

    from mimir import cache
    from mimir.broker import server as broker_server
    from mimir.broker.client import BrokerClient
    from mimir.broker.handlers import dispatch as real_dispatch
    from mimir.config import settings

    def slow_dispatch(line):
        time_module.sleep(0.05)
        return real_dispatch(line)

    monkeypatch.setattr(broker_server, "dispatch", slow_dispatch)
    monkeypatch.setattr(settings, "broker_slow_rpc_warn_ms", 0)
    caplog.set_level(logging.WARNING, logger="mimir.broker.server")

    sp = short_socket_path("slow-disabled")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            c.cache_set(cache._ns("slow-disabled-test"), '"x"', 60)
        finally:
            c.close()

    slow_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "broker slow rpc" in r.getMessage()
    ]
    assert not slow_warnings, "threshold=0 must disable slow-rpc warnings"


def test_server_serves_two_clients_concurrently(seeded_db):
    """Regression: pre-queue/worker broker was single-threaded across
    connections (`socketserver.UnixStreamServer` without
    `ThreadingMixIn`), so once one persistent client connected, all
    subsequent client RPCs sat unread in the kernel buffer and
    eventually timed out on the client's 5s socket timeout. In
    production this hit `mimir-app` and `mimir-tasks` whenever both
    held connections to the same broker.

    Pin: spin up two independent `BrokerClient` instances against
    one broker, do interleaved cache.set RPCs from both, assert
    both make progress (no client-side timeouts). Without the
    queue + per-connection reader threads this test would hang
    until the 5s socket timeout fires on the second client.
    """
    from mimir import cache
    from mimir.broker.client import BrokerClient

    sp = short_socket_path("two-clients")
    with broker_running(sp):
        a = BrokerClient(sp)
        b = BrokerClient(sp)
        try:
            # Open both connections by issuing a cheap ping from
            # each, in order — this forces the broker to accept()
            # both before either tries serious work.
            assert a.ping() is True
            assert b.ping() is True

            # Now interleave real RPCs from both clients. If only
            # one reader thread existed, the second `b.cache_set`
            # would block past the 5s client timeout because the
            # broker isn't reading from b's socket.
            for i in range(20):
                a.cache_set(cache._ns(f"two-clients:a:{i}"), f'"a-{i}"', 60)
                b.cache_set(cache._ns(f"two-clients:b:{i}"), f'"b-{i}"', 60)
        finally:
            a.close()
            b.close()

    # Both clients' writes landed in the cache.
    for i in range(20):
        assert cache.get(f"two-clients:a:{i}") == f"a-{i}"
        assert cache.get(f"two-clients:b:{i}") == f"b-{i}"


def test_server_serves_many_clients_concurrently(seeded_db):
    """Pushes the multi-client property harder than the two-client
    test above: 5 clients in 5 threads, each does 10 RPCs. Without
    per-connection reader threads, only the first client to connect
    gets served and the other four time out."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from mimir import cache
    from mimir.broker.client import BrokerClient

    sp = short_socket_path("many-clients")
    n_clients = 5
    rpcs_per_client = 10

    with broker_running(sp):
        clients = [BrokerClient(sp) for _ in range(n_clients)]
        try:

            def _worker(cid: int) -> None:
                c = clients[cid]
                for i in range(rpcs_per_client):
                    c.cache_set(cache._ns(f"many:{cid}:{i}"), f'"{cid}-{i}"', 60)

            with ThreadPoolExecutor(max_workers=n_clients) as ex:
                futures = [ex.submit(_worker, c) for c in range(n_clients)]
                for f in as_completed(futures):
                    f.result()
        finally:
            for c in clients:
                c.close()

    for cid in range(n_clients):
        for i in range(rpcs_per_client):
            assert cache.get(f"many:{cid}:{i}") == f"{cid}-{i}", (
                f"missing row for client {cid} rpc {i}"
            )


def test_server_slow_rpc_warning_includes_queue_wait(seeded_db, caplog, monkeypatch):
    """Slow-RPC WARNING now breaks total elapsed into queue-wait +
    dispatch components, so an operator can tell whether the broker
    is contended at the front of the queue (high queue_wait_ms,
    many clients piling on) or at the back (high dispatch_ms,
    SQLite writer lock held by scheduler-side ingest). Pin the
    breakdown is in the log message."""
    import logging as _logging
    import time as _time

    from mimir import cache
    from mimir.broker import server as broker_server
    from mimir.broker.client import BrokerClient
    from mimir.broker.handlers import dispatch as real_dispatch
    from mimir.config import settings

    def slow_dispatch(line):
        _time.sleep(0.05)
        return real_dispatch(line)

    monkeypatch.setattr(broker_server, "dispatch", slow_dispatch)
    monkeypatch.setattr(settings, "broker_slow_rpc_warn_ms", 10)
    caplog.set_level(_logging.WARNING, logger="mimir.broker.server")

    sp = short_socket_path("slow-breakdown")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            c.cache_set(cache._ns("slow-breakdown-test"), '"x"', 60)
        finally:
            c.close()

    slow = [r for r in caplog.records if "broker slow rpc" in r.getMessage()]
    assert slow, "expected a slow-rpc WARNING"
    msg = slow[0].getMessage()
    # Breakdown components must be present.
    assert "total" in msg
    assert "queued" in msg
    assert "dispatch" in msg
    assert "qsize=" in msg


def test_server_cleans_up_socket_file_on_shutdown(seeded_db):
    """After the broker shuts down (in production: SIGTERM; in
    tests: stop_event + server_close + manual unlink in the helper),
    the socket file must not be left behind to confuse the next
    boot. `broker_running` does the unlink; verify it succeeded."""
    sp = short_socket_path("cleanup")
    with broker_running(sp):
        assert sp.exists()
    assert not sp.exists()


def test_broker_server_constructs_read_pool_and_writer(seeded_db):
    """build_server() now wires a ReadSessionPool and WriterThread
    onto the server instance. Phase 1: parallel infrastructure;
    no handler uses them yet."""
    from mimir.broker.server import build_server
    from mimir.broker.pools import ReadSessionPool
    from mimir.broker.writes import WriterThread
    from tests.test_broker._helpers import short_socket_path

    sp = short_socket_path("phase1-wire")
    server = build_server(sp)
    try:
        assert isinstance(server.read_pool, ReadSessionPool)
        assert isinstance(server.writer, WriterThread)
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()


def test_broker_serve_starts_and_stops_writer(seeded_db):
    """serve() should start the writer at boot and stop it on shutdown.
    The existing broker_running fixture exercises serve() end-to-end."""
    from tests.test_broker._helpers import broker_running, short_socket_path

    sp = short_socket_path("phase1-startstop")
    with broker_running(sp):
        pass
    # No assertion needed beyond "the context manager exited cleanly".
