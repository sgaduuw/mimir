"""Concurrent ordinary replies must arrive as complete JSON frames."""

import json
import queue
import socket
import threading
import time

from mimir.broker.protocol import Reply
from mimir.broker.server import (
    REPLY_TIMEOUT_SEC,
    ClientConnection,
    _BrokerServer,
    _ReplyDelivery,
)


def test_concurrent_ordinary_replies_do_not_interleave():
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
    second_thread = None
    try:
        normal_thread.start()
        deadline = time.monotonic() + 2
        while not conn.send_lock.locked() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert conn.send_lock.locked()
        assert normal_thread.is_alive(), "ordinary reply must be transmitting"
        second_reply = Reply(rpc_id=2, ok=True)
        second_bytes = second_reply.model_dump_json().encode() + b"\n"
        second = _ReplyDelivery(
            b'{"rpc_id":2,"op":"ping"}',
            conn,
            queue.Queue(),
            "cache",
            0,
            time.perf_counter(),
        )
        second_thread = threading.Thread(
            target=server._deliver_reply, args=(second_reply, second)
        )
        second_thread.start()
        # The first frame exceeds the socket buffer and holds the send lock.
        # The second worker must wait rather than insert bytes into that frame.
        time.sleep(0.1)
        assert normal_thread.is_alive()
        assert second_thread.is_alive()
        while len(received) < len(normal_bytes) + len(second_bytes):
            chunk = receiver.recv(65536)
            if not chunk:
                break
            received.extend(chunk)
        second_thread.join(2)
        assert not second_thread.is_alive()
        normal_thread.join(2)
        assert not normal_thread.is_alive()
        assert bytes(received) == normal_bytes + second_bytes
        assert [json.loads(line)["rpc_id"] for line in received.splitlines()] == [1, 2]
    finally:
        sender.close()
        receiver.close()
        normal_thread.join(2)
        if second_thread is not None:
            second_thread.join(2)
