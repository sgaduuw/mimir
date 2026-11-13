"""Cache invalidation replies wait for commit without occupying the cache worker."""

import json
import socket
import threading

import pytest

from mimir import cache
from mimir.broker.writes import WriteOp
from mimir.extensions import SessionLocal
from mimir.models import CacheEntry
from tests.test_broker._helpers import broker_running, short_socket_path


@pytest.mark.parametrize(
    ("op", "arguments", "expected"),
    [
        ("cache_delete", {"key": "v3:deferred:alpha:one"}, None),
        ("cache_delete_for_inbox", {"name": "alpha"}, {"rows_deleted": 2}),
        ("cache_purge_expired", {}, {"rows_deleted": 2}),
    ],
)
def test_invalidation_does_not_hold_cache_worker(seeded_db, op, arguments, expected):
    """The normal socket entry preserves FIFO writes and commit acknowledgement."""
    key = "v3:deferred:alpha:one"
    with broker_running(short_socket_path("deferred")) as server:
        for nskey in (key, "v3:deferred:alpha:two"):
            cache._set_via_writer_for_nskey(server.writer, nskey, '"old"', -60).result(
                5
            )
        with SessionLocal() as session:
            assert session.get(CacheEntry, key) is not None
        release = threading.Event()
        started = threading.Event()

        def hold_writer(conn):
            started.set()
            assert release.wait(15)

        held = server.writer.submit(WriteOp(label="test:held-writer", fn=hold_writer))
        assert started.wait(5)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(server.server_address))
                requests = [
                    {"rpc_id": 1, "op": op, **arguments},
                    {
                        "rpc_id": 2,
                        "op": "cache_set",
                        "key": key,
                        "value_json": '"new"',
                        "ttl": 60,
                    },
                    {"rpc_id": 3, "op": "ping"},
                ]
                client.sendall(
                    b"".join(json.dumps(req).encode() + b"\n" for req in requests)
                )
                with client.makefile("rb") as reader:
                    replies = [json.loads(reader.readline()) for _ in range(2)]
                    assert {reply["rpc_id"] for reply in replies} == {2, 3}
                    assert all(reply["ok"] for reply in replies)
                    assert not held.done(), (
                        "writer must still be held when cache replies arrive"
                    )
                    release.set()
                    reply = json.loads(reader.readline())
                    assert reply["rpc_id"] == 1
                    assert reply["ok"] is True
                    assert reply["result"] == expected
            server.writer.submit(
                WriteOp(label="test:barrier", fn=lambda conn: None)
            ).result(5)
            with SessionLocal() as session:
                assert session.get(CacheEntry, key).value == '"new"'
        finally:
            release.set()
            held.result(5)


def _hold_writer(writer):
    release = threading.Event()
    started = threading.Event()

    def hold(conn):
        started.set()
        assert release.wait(15)

    future = writer.submit(WriteOp(label="test:held-writer", fn=hold))
    assert started.wait(5)
    return release, future


@pytest.mark.parametrize("parked_producer", [False, True])
@pytest.mark.parametrize(
    ("op", "arguments"),
    [
        ("cache_delete", {"key": "v3:full:alpha"}),
        ("cache_delete_for_inbox", {"name": "alpha"}),
        ("cache_purge_expired", {}),
    ],
)
def test_full_writer_queue_preserves_invalidation(
    seeded_db, monkeypatch, op, arguments, parked_producer
):
    import time

    from mimir.config import settings

    monkeypatch.setattr(settings, "broker_writer_queue_depth", 1)
    with broker_running(short_socket_path("full-delete")) as server:
        cache._set_via_writer_for_nskey(
            server.writer, "v3:full:alpha", '"old"', -60
        ).result(5)
        with SessionLocal() as session:
            assert session.get(CacheEntry, "v3:full:alpha") is not None
        release, held = _hold_writer(server.writer)
        filling = server.writer.submit(WriteOp(label="test:fill", fn=lambda conn: None))
        assert server.writer._queue.full()
        producer = None
        try:
            if parked_producer:
                producer = threading.Thread(
                    target=lambda: server.writer.submit(
                        WriteOp(label="test:producer", fn=lambda conn: None)
                    ),
                    daemon=True,
                )
                producer.start()
                # submit holds this lock across its blocking queue put.
                deadline = time.monotonic() + 2
                while (
                    not server.writer._stop_lock.locked()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                assert server.writer._stop_lock.locked()
                assert producer.is_alive()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(server.server_address))
                client.sendall(
                    json.dumps({"rpc_id": 1, "op": op, **arguments}).encode()
                    + b'\n{"rpc_id":2,"op":"ping"}\n'
                )
                import select

                # Saturated invalidations must wait for admission, not vanish.
                assert not select.select([client], [], [], 0.2)[0]
                assert not held.done()
                release.set()
                with client.makefile("rb") as reader:
                    replies = [json.loads(reader.readline()) for _ in range(2)]
                    assert {reply["rpc_id"] for reply in replies} == {1, 2}
                    assert all(reply["ok"] for reply in replies)
                    deleted = next(reply for reply in replies if reply["rpc_id"] == 1)
                    assert deleted["result"] == (
                        None if op == "cache_delete" else {"rows_deleted": 1}
                    )
                with SessionLocal() as session:
                    assert session.get(CacheEntry, "v3:full:alpha") is None
        finally:
            release.set()
            if producer is not None:
                producer.join(5)
                assert not producer.is_alive()
            held.result(5)
            filling.result(5)


@pytest.mark.parametrize("depth", [0, 1, 3])
def test_completion_capacity_uses_writer_setting(seeded_db, monkeypatch, depth):
    from mimir.config import settings

    monkeypatch.setattr(settings, "broker_writer_queue_depth", depth)
    with broker_running(short_socket_path("completion-capacity")) as server:
        assert server.completion_queue.maxsize == max(1, depth)


def test_completion_queue_overflow_is_bounded_and_honest(seeded_db, monkeypatch):
    import time

    from mimir.broker.handlers import PendingCacheReply
    from mimir.config import settings

    monkeypatch.setattr(settings, "broker_writer_queue_depth", 1)
    entered = threading.Event()
    release = threading.Event()
    original_resolve = PendingCacheReply.resolve

    def resolving(reply):
        if reply.rpc_id == 1:
            entered.set()
            assert release.wait(5)
        return original_resolve(reply)

    monkeypatch.setattr(PendingCacheReply, "resolve", resolving)
    with broker_running(short_socket_path("full-completion")) as server:
        keys = [f"v3:completion:{i}" for i in range(3)]
        for key in keys:
            cache._set_via_writer_for_nskey(server.writer, key, '"old"', 60).result(5)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(server.server_address))
                for rpc_id in (1, 2):
                    client.sendall(
                        json.dumps(
                            {
                                "rpc_id": rpc_id,
                                "op": "cache_delete",
                                "key": keys[rpc_id - 1],
                            }
                        ).encode()
                        + b"\n"
                    )
                    if rpc_id == 1:
                        assert entered.wait(2), (
                            "first reply must occupy completion worker"
                        )
                    else:
                        deadline = time.monotonic() + 2
                        while (
                            not server.completion_queue.full()
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.01)
                        assert server.completion_queue.qsize() == 1
                # Let the admitted DELETE drain its writer slot. The reply queue
                # stays full, exercising its production bound independently.
                server.writer.submit(
                    WriteOp(label="test:barrier", fn=lambda conn: None)
                ).result(5)
                requests = [
                    {"rpc_id": 3, "op": "cache_delete", "key": keys[2]},
                    {"rpc_id": 4, "op": "ping"},
                ]
                client.sendall(
                    b"".join(json.dumps(req).encode() + b"\n" for req in requests)
                )
                with client.makefile("rb") as reader:
                    rejected = json.loads(reader.readline())
                    assert rejected["rpc_id"] == 3
                    assert rejected["ok"] is False
                    assert rejected["error"] == "writer busy"
                    assert json.loads(reader.readline())["rpc_id"] == 4
                    assert server.completion_queue.qsize() == 1
                    release.set()
                    replies = [json.loads(reader.readline()) for _ in range(2)]
                    assert {reply["rpc_id"] for reply in replies} == {1, 2}
                    assert all(reply["ok"] for reply in replies)
            server.writer.submit(
                WriteOp(label="test:barrier", fn=lambda conn: None)
            ).result(5)
            # A rejected handoff is not a cancelled write, just as with timeout.
            with SessionLocal() as session:
                assert all(session.get(CacheEntry, key) is None for key in keys)
        finally:
            release.set()


def test_completion_deadlines_are_not_restarted_in_queue(seeded_db):
    import time

    with broker_running(short_socket_path("deadline")) as server:
        release, held = _hold_writer(server.writer)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(7)
                client.connect(str(server.server_address))
                requests = [
                    {"rpc_id": i, "op": "cache_purge_expired"} for i in range(1, 4)
                ]
                start = time.monotonic()
                client.sendall(
                    b"".join(json.dumps(req).encode() + b"\n" for req in requests)
                )
                with client.makefile("rb") as reader:
                    replies = [json.loads(reader.readline()) for _ in requests]
                elapsed = time.monotonic() - start
                assert 4.5 <= elapsed < 7, (
                    "each queued wait must share its original five-second budget"
                )
                assert {reply["rpc_id"] for reply in replies} == {1, 2, 3}
                assert all(
                    not reply["ok"] and reply["error"] == "writer busy"
                    for reply in replies
                )
                assert not held.done()
        finally:
            release.set()
            held.result(5)


def test_disconnect_and_shutdown_drain_pending_replies(seeded_db, monkeypatch):
    finished = []
    finalized = threading.Event()
    with broker_running(short_socket_path("stop-completion")) as server:
        original_finish = server._finish_request

        def finish(conn, work_queue):
            original_finish(conn, work_queue)
            finished.append((conn, conn.pending))
            finalized.set()

        monkeypatch.setattr(server, "_finish_request", finish)
        release, held = _hold_writer(server.writer)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(server.server_address))
                client.sendall(
                    b'{"rpc_id":1,"op":"cache_purge_expired"}\n{"rpc_id":2,"op":"cache_purge_expired"}\n{"rpc_id":3,"op":"ping"}\n'
                )
                with client.makefile("rb") as reader:
                    assert json.loads(reader.readline())["rpc_id"] == 3
            assert finalized.wait(2)
            assert len(finished) == 1
            conn = finished[0][0]
            assert conn.pending == 2
            assert server.cache_queue.unfinished_tasks == 2
            server.stop_event.set()
            release.set()
            for worker in server._cache_worker_threads:
                worker.join(2)
            server._completion_thread.join(2)
            assert not server._completion_thread.is_alive()
            assert len(finished) == 3
            assert conn.pending == 0
            assert server.cache_queue.unfinished_tasks == 0
            assert server.completion_queue.unfinished_tasks == 0
            assert not server.in_flight._jobs
        finally:
            release.set()
            held.result(5)


@pytest.mark.parametrize("already_done", [False, True])
def test_abandoned_completion_logs_eventual_failure(caplog, already_done):
    from concurrent.futures import Future

    from mimir.broker.handlers.cache import PendingCacheReply

    future = Future()
    if already_done:
        future.set_exception(RuntimeError("commit failed"))
    reply = PendingCacheReply(future, 1, "cache_delete", "cache_delete")
    assert reply.busy("completion queue full").ok is False
    if not already_done:
        future.set_exception(RuntimeError("commit failed"))
    assert "cache_delete failed after reply: commit failed" in caplog.text


@pytest.mark.parametrize("error", ["OperationalError", "HandlerCrashed"])
def test_commit_failure_keeps_wire_error_and_finishes_once(
    seeded_db, monkeypatch, error
):
    def failing_delete(writer, key, *, block=True):
        def fail(conn):
            if error == "OperationalError":
                conn.exec_driver_sql("SELECT * FROM missing_cache_test_table")
            raise RuntimeError("cache commit failed")

        return writer.submit(WriteOp(label="test:failed-delete", fn=fail), block=block)

    monkeypatch.setattr(cache, "delete_via_writer", failing_delete)
    with broker_running(short_socket_path("failed-completion")) as server:
        finished = threading.Event()
        finalizations = []
        original_finish = server._finish_request

        def finish(conn, work_queue):
            original_finish(conn, work_queue)
            finalizations.append((conn, work_queue))
            finished.set()

        monkeypatch.setattr(server, "_finish_request", finish)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(server.server_address))
            client.sendall(b'{"rpc_id":7,"op":"cache_delete","key":"v3:error"}\n')
            with client.makefile("rb") as reader:
                reply = json.loads(reader.readline())
                assert reply["rpc_id"] == 7
                assert reply["ok"] is False
                assert reply["error"] == error
                assert finished.wait(2)
                assert len(finalizations) == 1
                assert finalizations[0][0].pending == 0
                assert server.cache_queue.unfinished_tasks == 0
                client.sendall(b'{"rpc_id":8,"op":"ping"}\n')
                assert json.loads(reader.readline())["ok"] is True


@pytest.mark.parametrize("blocked_by", ["socket", "send_lock"])
def test_slow_client_does_not_hold_other_connections_completions(
    seeded_db, monkeypatch, blocked_by
):
    import os
    import time

    from mimir.broker import server as server_module

    with broker_running(short_socket_path("slow-completion")) as server:
        entered = threading.Event()
        sockets = []
        original_reader = server._reader_loop
        original_deliver = server._deliver_reply
        held_locks = []

        def read(sock):
            sockets.append(sock)
            original_reader(sock)

        def deliver(reply, delivery):
            if reply.rpc_id == 1 and not entered.is_set():
                conn = delivery.conn
                if blocked_by == "send_lock":
                    conn.send_lock.acquire()
                    held_locks.append(conn.send_lock)
                entered.set()
            return original_deliver(reply, delivery)

        monkeypatch.setattr(server, "_reader_loop", read)
        monkeypatch.setattr(server, "_deliver_reply", deliver)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as slow_client:
            slow_client.connect(str(server.server_address))
            deadline = time.monotonic() + 2
            while (
                not sockets or sockets[0].gettimeout() is None
            ) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert sockets[0].gettimeout() == server_module.REPLY_TIMEOUT_SEC
            if blocked_by == "socket":
                # Normal-entry accepted socket has a nonblocking OS fd beneath
                # Python's timeout mode. Fill its real output buffer completely.
                sockets[0].setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                filled = 0
                while True:
                    try:
                        filled += os.write(sockets[0].fileno(), b"x" * 16384)
                    except BlockingIOError:
                        break
                assert filled > 0
            try:
                slow_client.sendall(b'{"rpc_id":1,"op":"cache_purge_expired"}\n')
                assert entered.wait(2)
                with socket.socket(
                    socket.AF_UNIX, socket.SOCK_STREAM
                ) as healthy_client:
                    healthy_client.settimeout(2)
                    healthy_client.connect(str(server.server_address))
                    healthy_client.sendall(b'{"rpc_id":2,"op":"cache_purge_expired"}\n')
                    with healthy_client.makefile("rb") as reader:
                        reply = json.loads(reader.readline())
                        assert reply["rpc_id"] == 2
                        assert reply["ok"] is True
                assert sockets[0].gettimeout() == server_module.REPLY_TIMEOUT_SEC
            finally:
                for lock in held_locks:
                    lock.release()


def test_serve_warns_when_cache_completion_drain_is_incomplete(
    seeded_db, monkeypatch, caplog
):
    import logging
    from types import SimpleNamespace

    from mimir.broker import _context
    from mimir.broker import server as server_module

    saved_pool, saved_writer = _context._active_pool, _context._active_writer
    server = server_module.build_server(short_socket_path("incomplete-drain"))
    server.stop_event.set()
    server._completion_thread.join(2)
    assert not server._completion_thread.is_alive()
    # Model a completion worker that remains alive after its bounded join.
    server._completion_thread = SimpleNamespace(
        join=lambda timeout: None, is_alive=lambda: True
    )
    server.completion_queue.put(object())
    monkeypatch.setattr(server_module, "build_server", lambda path: server)
    monkeypatch.setattr(server_module, "_register_stack_dump_signal", lambda: None)
    monkeypatch.setattr(server_module.signal, "signal", lambda *args: None)
    monkeypatch.setattr(server, "serve_forever", lambda **kwargs: None)
    try:
        with caplog.at_level(logging.INFO, logger="mimir.broker.server"):
            server_module.serve(short_socket_path("incomplete-drain"))
    finally:
        _context.set_active(saved_pool, saved_writer)
    messages = [record.getMessage() for record in caplog.records]
    assert any("incomplete cache reply drain" in message for message in messages)
    assert "broker: shut down cleanly" not in messages


def test_brief_send_lock_contention_retries_count_reply_once(
    seeded_db, monkeypatch, caplog
):
    import logging
    import time

    entered = threading.Event()
    finalized = threading.Event()
    finalizations = []
    timers = []
    with broker_running(short_socket_path("retry-completion")) as server:
        for key in ("v3:retry:one", "v3:retry:two"):
            cache._set_via_writer_for_nskey(server.writer, key, '"old"', -60).result(5)
        original_deliver = server._deliver_reply
        original_finish = server._finish_request
        attempts = []

        def deliver(reply, delivery):
            if reply.rpc_id == 101:
                attempts.append(reply)
                if not entered.is_set():
                    delivery.conn.send_lock.acquire()
                    timer = threading.Timer(0.25, delivery.conn.send_lock.release)
                    timers.append(timer)
                    timer.start()
                    entered.set()
            return original_deliver(reply, delivery)

        def finish(conn, work_queue):
            original_finish(conn, work_queue)
            finalizations.append(conn)
            if len(finalizations) == 2:
                finalized.set()

        monkeypatch.setattr(server, "_deliver_reply", deliver)
        monkeypatch.setattr(server, "_finish_request", finish)
        with caplog.at_level(logging.WARNING, logger="mimir.broker.server"):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(server.server_address))
                client.sendall(b'{"rpc_id":101,"op":"cache_purge_expired"}\n')
                assert entered.wait(2)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as other:
                    other.settimeout(2)
                    other.connect(str(server.server_address))
                    other.sendall(b'{"rpc_id":102,"op":"cache_purge_expired"}\n')
                    with other.makefile("rb") as reader:
                        assert json.loads(reader.readline())["rpc_id"] == 102
                with client.makefile("rb") as reader:
                    reply = json.loads(reader.readline())
                    assert reply["rpc_id"] == 101
                    assert reply["ok"] is True
                    assert reply["result"] == {"rows_deleted": 2}
                assert finalized.wait(2)
                deadline = time.monotonic() + 2
                while (
                    server.completion_queue.unfinished_tasks
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                assert len(attempts) >= 2
                assert len(finalizations) == 2
                assert len({id(conn) for conn in finalizations}) == 2
                assert all(conn.pending == 0 for conn in finalizations)
                assert server.completion_queue.unfinished_tasks == 0
        for timer in timers:
            timer.join(2)
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("broker slow rpc")
        ]
        assert sum('"rpc_id":101' in message for message in warnings) <= 1


@pytest.mark.parametrize("reason", ["deadline", "queue_full"])
def test_abandoned_reply_finishes_once_and_keeps_connection_usable(
    seeded_db, monkeypatch, reason
):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from mimir.broker import handlers
    from mimir.broker.client import BrokerClient, BrokerUnavailable
    from mimir.broker.protocol import CacheDeleteForInboxRequest, PingRequest
    from mimir.config import settings

    if reason == "queue_full":
        monkeypatch.setattr(settings, "broker_writer_queue_depth", 1)
    model, original_handler = handlers._DISPATCH["cache_delete_for_inbox"]

    def handle(req):
        pending = original_handler(req)
        if reason == "deadline":
            pending.deadline = time.monotonic() + 0.25
        return pending

    monkeypatch.setitem(handlers._DISPATCH, "cache_delete_for_inbox", (model, handle))
    with broker_running(short_socket_path("abandoned-retry")) as server:
        original_deliver = server._deliver_reply
        original_finish = server._finish_request
        entered = threading.Event()
        allow_attempt = threading.Event()
        finalized = threading.Event()
        blocked_connections = []
        finishes = []

        def deliver(reply, delivery):
            if reply.rpc_id == 1 and not entered.is_set():
                delivery.conn.send_lock.acquire()
                blocked_connections.append(delivery.conn)
                entered.set()
                assert allow_attempt.wait(2)
            return original_deliver(reply, delivery)

        def finish(conn, work_queue):
            original_finish(conn, work_queue)
            if conn in blocked_connections:
                finishes.append(conn)
                finalized.set()

        monkeypatch.setattr(server, "_deliver_reply", deliver)
        monkeypatch.setattr(server, "_finish_request", finish)
        client = BrokerClient(short_socket_path("abandoned-retry"))
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                waiting = pool.submit(
                    client.call, CacheDeleteForInboxRequest(name="alpha"), timeout=0.8
                )
                assert entered.wait(2)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as other:
                    other.settimeout(2)
                    other.connect(str(server.server_address))
                    other.sendall(b'{"rpc_id":99,"op":"cache_purge_expired"}\n')
                    deadline = time.monotonic() + 2
                    while (
                        not server.completion_queue.qsize()
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    assert server.completion_queue.qsize() == 1
                    allow_attempt.set()
                    with other.makefile("rb") as reader:
                        assert json.loads(reader.readline())["rpc_id"] == 99
                assert finalized.wait(2)
                with pytest.raises(BrokerUnavailable, match="rpc timed out"):
                    waiting.result(timeout=2)
            assert len(finishes) == 1
            assert blocked_connections[0].pending == 0
            assert server.cache_queue.unfinished_tasks == 0
            assert server.completion_queue.unfinished_tasks == 0
            with client._pending_lock:
                assert not client._pending
            blocked_connections.pop().send_lock.release()
            assert client.call(PingRequest()) == {}
        finally:
            allow_attempt.set()
            for conn in blocked_connections:
                conn.send_lock.release()
            client.close()
