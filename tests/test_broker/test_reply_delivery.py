"""A deferred reply must not truncate a concurrent ordinary reply."""

import json
import queue
import socket
import threading
import time

import pytest

from mimir.broker.protocol import Reply
from mimir.broker.server import (
    REPLY_TIMEOUT_SEC,
    ClientConnection,
    _BrokerServer,
    _ReplyDelivery,
)


@pytest.mark.parametrize("concurrent_deferred", [False, True])
def test_deferred_lock_budget_does_not_abort_ordinary_reply(concurrent_deferred):
    server = object.__new__(_BrokerServer)
    sender, receiver = socket.socketpair()
    sender.settimeout(REPLY_TIMEOUT_SEC)
    sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    receiver.settimeout(2)
    conn = ClientConnection(sender)
    normal = Reply(rpc_id=1, ok=True, result={"data": "x" * (512 * 1024)})
    normal_bytes = normal.model_dump_json().encode() + b"\n"
    delivery = _ReplyDelivery(
        b'{"rpc_id":1,"op":"warm"}', conn, queue.Queue(), "warm", 0, time.perf_counter()
    )
    normal_thread = threading.Thread(
        target=server._deliver_reply, args=(normal, delivery)
    )
    received = bytearray()
    try:
        normal_thread.start()
        deadline = time.monotonic() + 2
        while not conn.send_lock.locked() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert conn.send_lock.locked()
        assert normal_thread.is_alive(), "ordinary reply must be transmitting"
        deferred = _ReplyDelivery(
            b'{"rpc_id":2,"op":"cache_purge_expired"}',
            conn,
            queue.Queue(),
            "cache",
            0,
            time.perf_counter(),
            deferred=True,
        )
        # A brief consumer scheduling pause is far below the ordinary 30 s budget.
        if concurrent_deferred:
            server._deliver_reply(Reply(rpc_id=2, ok=True), deferred)
        else:
            time.sleep(0.1)
        while len(received) < len(normal_bytes):
            chunk = receiver.recv(65536)
            if not chunk:
                break
            received.extend(chunk)
        normal_thread.join(2)
        assert not normal_thread.is_alive()
        assert bytes(received[: len(normal_bytes)]) == normal_bytes, (
            f"deferred lock timeout truncated ordinary frame: {len(received)} / {len(normal_bytes)} bytes"
        )
        assert json.loads(received.splitlines()[0])["rpc_id"] == 1
    finally:
        sender.close()
        receiver.close()
        normal_thread.join(2)
