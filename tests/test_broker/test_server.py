"""Tests for `mimir.broker.server`'s lifecycle: socket binding,
accept loop, clean shutdown, stale-socket cleanup. Uses an
in-thread broker on a tmp UNIX socket. RPC content is tested
in `test_handlers.py`; here we care about the socket lifecycle
and the bytes-on-the-wire framing."""

import socket
import time
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
        reply = _send_line(sp, b'{"rpc_id":1,"op":"ping"}')
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
                wfile.write(b'{"rpc_id":1,"op":"ping"}\n')
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


def test_reader_threads_prunes_dead_threads(seeded_db):
    """Each dead reader thread (peer closed connection, idle timeout,
    or error exit) must be removed from `_BrokerServer._reader_threads`.
    Otherwise the list grows unboundedly over the broker's lifetime
    as connections churn (gunicorn workers reconnecting, scheduler-
    tasks pings, broker-ping healthchecks). Diagnosed on prod 2026-06-11
    via the v3.1.0 tracemalloc instrumentation.

    Pin: open 20 short-lived client connections in sequence, close each
    cleanly, wait for the reader threads to notice EOF and exit, and
    assert the list retains no dead threads."""
    from mimir.broker.client import BrokerClient

    sp = short_socket_path("reader-prune")
    n_clients = 20

    with broker_running(sp) as server:
        for _ in range(n_clients):
            c = BrokerClient(sp)
            assert c.ping() is True
            c.close()

        # Reader threads notice EOF within SHUTDOWN_POLL_SEC (0.1s) of
        # the peer close; allow ample slack for CI scheduling.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            snapshot = list(server._reader_threads)
            dead = [t for t in snapshot if not t.is_alive()]
            if not dead:
                break
            time.sleep(0.1)

        snapshot = list(server._reader_threads)
        dead_after = [t for t in snapshot if not t.is_alive()]
        assert dead_after == [], (
            f"_reader_threads retains {len(dead_after)} dead threads "
            f"after all connections closed: "
            f"{[t.name for t in dead_after]}"
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


def test_serve_sets_active_context_during_run(seeded_db):
    """While serve() is running, _context.get_active_pool() and
    get_active_writer() return the server's instances. After
    shutdown, the previous context is restored.

    Note: the session-scoped _session_broker fixture in conftest.py
    registers its pool+writer for the entire test session. The
    broker_running context manager saves and restores the previous
    context on exit, so after broker_running exits, the session
    broker's context is back in place (not cleared)."""
    from mimir.broker import _context
    from tests.test_broker._helpers import broker_running, short_socket_path

    # Capture the pre-test session-broker context.
    pre_pool = _context._active_pool
    pre_writer = _context._active_writer

    sp = short_socket_path("phase2-ctx")
    with broker_running(sp) as server:
        assert _context.get_active_pool() is server.read_pool
        assert _context.get_active_writer() is server.writer

    # After context manager exits, context is restored to what it was
    # before broker_running was entered (the session broker's context).
    assert _context._active_pool is pre_pool
    assert _context._active_writer is pre_writer


def test_start_workers_honors_per_queue_settings(seeded_db, monkeypatch):
    """`start_workers()` spawns `broker_<queue>_workers` threads per
    queue. Defaults: 1 cache, 1 long, 4 warm; bumping the env-tunable
    fields produces the expected counts. Threads are tagged with their
    own name (`broker-<label>-worker`) so the operator can spot which
    queue is busy in `ps`/thread-name dumps."""
    from mimir.broker.server import build_server
    from mimir.config import settings
    from tests.test_broker._helpers import short_socket_path

    monkeypatch.setattr(settings, "broker_cache_workers", 3)
    monkeypatch.setattr(settings, "broker_long_workers", 2)
    monkeypatch.setattr(settings, "broker_warm_workers", 5)

    sp = short_socket_path("tunable-workers")
    server = build_server(sp)  # build_server() spawns the worker pool
    try:
        assert len(server._cache_worker_threads) == 3
        assert len(server._long_worker_threads) == 2
        assert len(server._warm_worker_threads) == 5
        # Multi-worker queues get suffixed names; single-worker keeps
        # the bare prefix (see `start_workers` label logic).
        names = {t.name for t in server._cache_worker_threads}
        assert names == {
            "broker-cache-0-worker",
            "broker-cache-1-worker",
            "broker-cache-2-worker",
        }
        # All threads are running.
        for t in (
            server._cache_worker_threads
            + server._long_worker_threads
            + server._warm_worker_threads
        ):
            assert t.is_alive(), f"worker {t.name} should be running"
            assert t.daemon, f"worker {t.name} must be daemon for clean shutdown"
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()


def test_start_workers_single_worker_keeps_bare_label(seeded_db, monkeypatch):
    """When a queue's worker count is 1 (the default for cache and
    long), the thread name keeps the bare prefix (`broker-cache-worker`)
    rather than the suffixed form (`broker-cache-0-worker`). Matters
    for log/ps readability and to match the pre-tunable thread name."""
    from mimir.broker.server import build_server
    from mimir.config import settings
    from tests.test_broker._helpers import short_socket_path

    monkeypatch.setattr(settings, "broker_cache_workers", 1)
    monkeypatch.setattr(settings, "broker_long_workers", 1)
    monkeypatch.setattr(settings, "broker_warm_workers", 1)

    sp = short_socket_path("tunable-single")
    server = build_server(sp)
    try:
        assert [t.name for t in server._cache_worker_threads] == ["broker-cache-worker"]
        assert [t.name for t in server._long_worker_threads] == ["broker-long-worker"]
        assert [t.name for t in server._warm_worker_threads] == ["broker-warm-worker"]
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()


# Task 5 of the fast/slow tier split: warm_queue migrates from
# `queue.Queue` to `queue.PriorityQueue` so fast-tier RPCs dequeue
# ahead of queued slow-tier RPCs. The reader-side extractor
# (`_extract_warm_priority`) parses the wire-side `priority` field
# on each warm line; the worker's `get()` returns the smallest
# priority first (PriorityQueue compares tuples element-wise, so
# equal priority falls through to the enqueued_at tie-breaker).


def test_warm_queue_is_priority_queue(seeded_db):
    """warm_queue migrates to queue.PriorityQueue so fast-tier
    RPCs jump ahead of queued slow-tier RPCs."""
    import queue as _queue

    from mimir.broker.server import build_server
    from tests.test_broker._helpers import short_socket_path

    sp = short_socket_path("warm-priority-type")
    server = build_server(sp)
    try:
        assert isinstance(server.warm_queue, _queue.PriorityQueue)
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()


def test_warm_queue_dequeues_fast_priority_first(seeded_db, monkeypatch):
    """Two items, slow then fast: fast dequeues first.

    Stops the worker thread before staging items so the warm
    worker doesn't race the test by dequeuing and dispatching the
    stub-sock items (which would AttributeError on sendall(None)).
    """
    import time

    from mimir.broker.server import SHUTDOWN_POLL_SEC, build_server
    from mimir.config import settings
    from tests.test_broker._helpers import short_socket_path

    monkeypatch.setattr(settings, "broker_warm_workers", 1)
    sp = short_socket_path("warm-priority-order")
    server = build_server(sp)
    try:
        # Stop workers + wait one poll cycle for them to exit before
        # touching the queue so the warm worker can't dispatch our
        # stub items with sock=None.
        server.stop_event.set()
        for t in server._warm_worker_threads:
            t.join(timeout=SHUTDOWN_POLL_SEC * 5)
        while not server.warm_queue.empty():
            server.warm_queue.get_nowait()
        # Slow then fast: PriorityQueue must reorder so fast
        # dequeues first regardless of enqueue order.
        server.warm_queue.put(
            (
                1,
                time.perf_counter(),
                b'{"op":"warm_inbox","inbox_name":"x","priority":1}',
                None,
            )
        )
        server.warm_queue.put(
            (
                0,
                time.perf_counter(),
                b'{"op":"warm_inbox","inbox_name":"y","priority":0}',
                None,
            )
        )
        first = server.warm_queue.get_nowait()
        second = server.warm_queue.get_nowait()
        assert first[0] == 0
        assert second[0] == 1
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()


def test_warm_queue_preserves_fifo_within_priority(seeded_db):
    """Two items at same priority dequeue in put-order (FIFO via
    enqueued_at tiebreaker)."""
    import time

    from mimir.broker.server import SHUTDOWN_POLL_SEC, build_server
    from tests.test_broker._helpers import short_socket_path

    sp = short_socket_path("warm-priority-fifo")
    server = build_server(sp)
    try:
        # Stop workers + wait one poll cycle (see priority-order
        # test for the same reasoning).
        server.stop_event.set()
        for t in server._warm_worker_threads:
            t.join(timeout=SHUTDOWN_POLL_SEC * 5)
        while not server.warm_queue.empty():
            server.warm_queue.get_nowait()
        t1 = time.perf_counter()
        t2 = t1 + 0.001
        server.warm_queue.put((1, t1, b'{"op":"warm_inbox","inbox_name":"a"}', None))
        server.warm_queue.put((1, t2, b'{"op":"warm_inbox","inbox_name":"b"}', None))
        first = server.warm_queue.get_nowait()
        second = server.warm_queue.get_nowait()
        assert first[1] == t1
        assert second[1] == t2
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()


def test_build_server_warns_when_site_base_url_unset(seeded_db, caplog, monkeypatch):
    """Layer 1 of the config-drift guard family: when SITE_BASE_URL
    is empty, the broker logs a one-line WARNING at startup naming
    the affected feature (sitemap warming) so an operator reading
    `podman logs mimir-broker` sees the misconfig immediately.
    Driven by the 2026-06-01 production incident."""
    import logging as _logging

    from mimir.broker.server import build_server
    from mimir.config import settings as _settings
    from tests.test_broker._helpers import short_socket_path

    monkeypatch.setattr(_settings, "site_base_url", "")
    caplog.set_level(_logging.WARNING, logger="mimir.broker.server")

    sp = short_socket_path("site-base-url-unset")
    server = build_server(sp)
    try:
        site_warnings = [
            r
            for r in caplog.records
            if r.levelno == _logging.WARNING
            and "SITE_BASE_URL is unset" in r.getMessage()
        ]
        assert site_warnings, (
            "expected a SITE_BASE_URL WARNING at broker startup; "
            f"got: {[r.getMessage() for r in caplog.records]}"
        )
        msg = site_warnings[0].getMessage()
        # The message names the feature that's broken and the env
        # var to set, so an operator can act on the one-liner alone.
        assert "sitemap" in msg.lower()
        assert "SITE_BASE_URL" in msg
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()


def test_build_server_silent_when_site_base_url_set(seeded_db, caplog, monkeypatch):
    """Inverse pin: with SITE_BASE_URL set, the startup WARNING
    must not fire. Healthy deploys should not log noise on every
    broker bounce."""
    import logging as _logging

    from mimir.broker.server import build_server
    from mimir.config import settings as _settings
    from tests.test_broker._helpers import short_socket_path

    monkeypatch.setattr(_settings, "site_base_url", "https://example.com")
    caplog.set_level(_logging.WARNING, logger="mimir.broker.server")

    sp = short_socket_path("site-base-url-set")
    server = build_server(sp)
    try:
        site_warnings = [
            r
            for r in caplog.records
            if r.levelno == _logging.WARNING
            and "SITE_BASE_URL is unset" in r.getMessage()
        ]
        assert not site_warnings, (
            "no SITE_BASE_URL warning expected when the env is set"
        )
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()


def test_extract_warm_priority_parses_zero_one_and_default(seeded_db):
    """The reader-side priority extractor handles 0, 1, and
    missing field (returns 1). Bounded to the line head so payload
    size doesn't slow the reader thread."""
    from mimir.broker.server import _extract_warm_priority

    assert _extract_warm_priority(b'{"op":"warm_inbox","priority":0}') == 0
    assert _extract_warm_priority(b'{"op":"warm_inbox","priority":1}') == 1
    assert _extract_warm_priority(b'{"op":"warm_inbox"}') == 1
    # Whitespace tolerance:
    assert _extract_warm_priority(b'{"op":"warm_inbox", "priority": 0}') == 0
    # Malformed priority value (non-digit) falls back to slow.
    assert _extract_warm_priority(b'{"op":"warm_inbox","priority":"nope"}') == 1


def test_concurrent_worker_replies_are_not_torn():
    """Two workers sending replies on the same connection
    simultaneously: each reply lands as a complete, parseable JSON
    line. Pins the per-connection _send_lock (3.0.0 pipelining)."""
    import socket
    import threading
    from mimir.broker.server import ClientConnection
    from mimir.broker.protocol import Reply

    # Create a connected socket pair: `peer` reads what `sock` writes.
    sock, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn = ClientConnection(sock=sock)
        reply_a = Reply(rpc_id=1, ok=True).model_dump_json().encode() + b"\n"
        reply_b = Reply(rpc_id=2, ok=True).model_dump_json().encode() + b"\n"

        def send(payload):
            with conn.send_lock:
                conn.sock.sendall(payload)

        # Fire two senders concurrently. The lock must serialise
        # them at the byte level.
        t1 = threading.Thread(target=send, args=(reply_a,))
        t2 = threading.Thread(target=send, args=(reply_b,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Read everything available; parse line-by-line.
        peer.settimeout(2.0)
        rfile = peer.makefile("rb")
        lines = [rfile.readline(), rfile.readline()]
        replies = [Reply.model_validate_json(line) for line in lines]
        assert {r.rpc_id for r in replies} == {1, 2}
    finally:
        sock.close()
        peer.close()
