"""Broker-only invalidations keep commit barriers without a wire endpoint."""

import json
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

import pytest

from mimir import cache
from mimir.broker.client import BrokerClient
from mimir.broker.protocol import CacheSetRequest, PingRequest
from mimir.broker.writes import WriteOp
from mimir.extensions import SessionLocal
from mimir.models import CacheEntry
from tests.test_broker._helpers import broker_running, short_socket_path

INVALIDATIONS = [
    (lambda: cache.delete("invalidate:alpha:one"), 1),
    (lambda: cache.delete_for_inbox("alpha"), 2),
]


@pytest.mark.parametrize("invalidate,expected", INVALIDATIONS)
def test_internal_invalidation_waits_through_saturation_and_commits(
    seeded_db, monkeypatch, invalidate, expected
):
    from mimir.config import settings

    monkeypatch.setattr(settings, "broker_writer_queue_depth", 1)
    with broker_running(short_socket_path("internal-invalidation")) as server:
        key = cache._ns("invalidate:alpha:one")
        for nskey in (key, cache._ns("invalidate:alpha:two")):
            cache._set_via_writer_for_nskey(server.writer, nskey, '"old"', -60).result(
                5
            )
        with SessionLocal() as session:
            assert session.get(CacheEntry, key).value == '"old"'
        # Start the writer connection as a broker before exercising helper routing
        # with non-broker settings; otherwise the connection is rightly read-only.
        monkeypatch.setattr(settings, "mimir_is_broker", False)
        release, entered = threading.Event(), threading.Event()

        def hold_writer(conn):
            entered.set()
            assert release.wait(10)

        held = server.writer.submit(WriteOp(label="test:held", fn=hold_writer))
        assert entered.wait(2)
        filling = server.writer.submit(WriteOp(label="test:fill", fn=lambda conn: None))
        assert server.writer._queue.full()
        with ThreadPoolExecutor(max_workers=1) as pool:
            invalidation = pool.submit(invalidate)
            try:
                deadline = time.monotonic() + 2
                while (
                    not server.writer._stop_lock.locked()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.001)
                assert server.writer._stop_lock.locked(), (
                    "invalidation must await admission"
                )
                assert not invalidation.done()
                client = BrokerClient(server.server_address)
                try:
                    # Neither ping nor set admission waits for this internal DELETE.
                    assert client.call(PingRequest(), timeout=2) == {}
                    reply = client._rpc(
                        CacheSetRequest(key=key, value_json='"busy"', ttl=60), timeout=2
                    )
                    assert (reply.ok, reply.error) == (False, "writer busy")
                    assert not held.done()
                finally:
                    client.close()
                release.set()
                assert invalidation.result(5) == expected
                # Returned counts are commit acknowledgements, not admissions.
                with SessionLocal() as session:
                    assert session.get(CacheEntry, key) is None
                cache._set_via_writer_for_nskey(
                    server.writer, key, '"fresh"', 60
                ).result(5)
                assert cache.get("invalidate:alpha:one") == "fresh"
            finally:
                release.set()
                held.result(5)
                filling.result(5)


@pytest.mark.parametrize("invalidate,expected", INVALIDATIONS)
def test_internal_invalidation_propagates_commit_failure(
    monkeypatch, invalidate, expected
):
    from mimir.broker._context import get_active_writer

    failed = Future()
    failed.set_exception(RuntimeError("commit failed"))
    monkeypatch.setattr(get_active_writer(), "submit", lambda op, block=True: failed)
    with pytest.raises(RuntimeError, match="commit failed"):
        invalidate()


@pytest.mark.parametrize(
    "op", ["cache_delete", "cache_delete_for_inbox", "cache_purge_expired"]
)
def test_retired_operation_leaves_rows_and_connection_usable(seeded_db, op):
    key = cache._ns("retired:alpha")
    with broker_running(short_socket_path("retired-op")) as server:
        cache._set_via_writer_for_nskey(server.writer, key, '"old"', -60).result(5)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(server.server_address)
            client.sendall(
                json.dumps(
                    {"rpc_id": 7, "op": op, "key": key, "name": "alpha"}
                ).encode()
                + b'\n{"rpc_id":8,"op":"ping"}\n'
            )
            with client.makefile("rb") as reader:
                rejected, ping = [json.loads(reader.readline()) for _ in range(2)]
            assert (rejected["rpc_id"], rejected["ok"], rejected["error"]) == (
                7,
                False,
                "UnknownOp",
            )
            assert (ping["rpc_id"], ping["ok"]) == (8, True)
        with SessionLocal() as session:
            assert session.get(CacheEntry, key).value == '"old"'
        server.cache_queue.join()
