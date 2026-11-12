"""#1157: handle_cache_set must not block when another producer is
already blocked on the full writer queue (submit used to hold
_stop_lock across a blocking put)."""


def test_cache_set_handler_does_not_wait_behind_blocked_producer(broker_active):
    import queue
    import threading
    import time

    from mimir.broker import _context
    from mimir.broker.handlers.cache import handle_cache_set
    from mimir.broker.protocol import CacheSetRequest
    from mimir.broker.writes import WriteOp

    writer = _context.get_active_writer()
    release = threading.Event()
    started = threading.Event()

    def _hold(conn):
        started.set()
        release.wait(timeout=15)

    futures = [writer.submit(WriteOp(label="test:hold", fn=_hold))]
    producer = None
    try:
        assert started.wait(timeout=5)
        while True:
            try:
                futures.append(
                    writer.submit(
                        WriteOp(label="test:fill", fn=lambda c: None), block=False
                    )
                )
            except queue.Full:
                break

        # A blocking producer (stands in for a warm-run cache.set) parks
        # in put on the full queue.
        def _blocking_producer():
            futures.append(
                writer.submit(WriteOp(label="test:warm-like", fn=lambda c: None))
            )

        producer = threading.Thread(target=_blocking_producer, daemon=True)
        producer.start()
        time.sleep(0.2)
        assert producer.is_alive(), "producer should be parked on the full queue"

        result = {}

        def _call():
            result["reply"] = handle_cache_set(
                CacheSetRequest(
                    rpc_id=1, key="v3:behind-producer", value_json='"x"', ttl=60
                )
            )

        caller = threading.Thread(target=_call, daemon=True)
        caller.start()
        caller.join(timeout=2.0)
        assert not caller.is_alive(), (
            "handle_cache_set still blocked after 2 s behind a producer "
            "parked on the full writer queue"
        )
        assert result["reply"].ok is False
        assert result["reply"].error == "writer busy"
    finally:
        release.set()
        if producer is not None:
            producer.join(timeout=10)
        for f in list(futures):
            f.result(timeout=10)
