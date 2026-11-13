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
def test_full_writer_queue_rejects_without_holding_cache_worker(
    seeded_db, monkeypatch, op, arguments, parked_producer
):
    import time

    from mimir.config import settings

    monkeypatch.setattr(settings, "broker_writer_queue_depth", 1)
    with broker_running(short_socket_path("full-delete")) as server:
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
                with client.makefile("rb") as reader:
                    rejected = json.loads(reader.readline())
                    assert rejected["rpc_id"] == 1
                    assert rejected["ok"] is False
                    assert rejected["error"] == "writer busy"
                    assert json.loads(reader.readline())["ok"] is True
                    assert not held.done()
        finally:
            release.set()
            if producer is not None:
                producer.join(5)
                assert not producer.is_alive()
            held.result(5)
            filling.result(5)


def test_completion_queue_overflow_is_bounded_and_honest(seeded_db, monkeypatch):
    from mimir.broker.handlers.cache import PendingCacheReply

    entered = threading.Event()
    original_resolve = PendingCacheReply.resolve

    def resolving(reply):
        entered.set()
        return original_resolve(reply)

    monkeypatch.setattr(PendingCacheReply, "resolve", resolving)
    with broker_running(short_socket_path("full-completion")) as server:
        server.completion_queue.maxsize = 1
        keys = [f"v3:completion:{i}" for i in range(3)]
        for key in keys:
            cache._set_via_writer_for_nskey(server.writer, key, '"old"', 60).result(5)
        release, held = _hold_writer(server.writer)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(server.server_address))
                client.sendall(
                    json.dumps(
                        {"rpc_id": 1, "op": "cache_delete", "key": keys[0]}
                    ).encode()
                    + b"\n"
                )
                assert entered.wait(2), (
                    "first commit wait must occupy completion worker"
                )
                requests = [
                    {"rpc_id": 2, "op": "cache_delete", "key": keys[1]},
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
                    assert not held.done()
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
            held.result(5)


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
    reply = PendingCacheReply(future, 1, "cache_delete")
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
