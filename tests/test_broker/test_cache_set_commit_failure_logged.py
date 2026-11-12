"""#1157: handle_cache_set no longer reads the WriteFuture, so a failed
commit must still leave a log line."""


def test_cache_set_handler_logs_commit_failure(broker_active, caplog):
    import logging
    import time
    from unittest.mock import patch

    from mimir.broker.handlers.cache import handle_cache_set
    from mimir.broker.protocol import CacheSetRequest
    from mimir.broker.writes import WriteOp

    submitted = []

    def _failing_set(w, nskey, value_json, ttl, *, block=True):
        def _boom(conn):
            raise RuntimeError("disk I/O error")

        f = w.submit(WriteOp(label=f"cache.set:{nskey}", fn=_boom), block=block)
        submitted.append(f)
        return f

    key = "v3:commit-fails"
    with caplog.at_level(logging.WARNING, logger="mimir.broker.handlers.cache"):
        with patch("mimir.cache._set_via_writer_for_nskey", _failing_set):
            reply = handle_cache_set(
                CacheSetRequest(rpc_id=1, key=key, value_json='"x"', ttl=60)
            )
        assert reply.ok is True
        deadline = time.monotonic() + 5
        # The done callback runs on the writer thread just after the
        # future resolves, so poll the log, not the future.
        while (
            f"cache_set failed (key={key})" not in caplog.text
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert submitted[0].exception(timeout=1) is not None
        assert f"cache_set failed (key={key})" in caplog.text
