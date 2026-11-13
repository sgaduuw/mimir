"""Broker-owned cleanup cadence and lifecycle."""

import queue
import threading
from concurrent.futures import Future
from unittest.mock import Mock

import pytest

from mimir import cache
from mimir.broker import server


def test_each_broker_start_purges_before_waiting(monkeypatch):
    """Restarting sooner than the interval must not postpone cleanup forever."""
    for _ in range(2):
        stop = threading.Event()
        writer = object()
        future = Future()
        future.set_result(0)

        def submit(actual_writer, *, block):
            assert actual_writer is writer
            assert block is False
            stop.set()
            return future

        purge = Mock(side_effect=submit)
        monkeypatch.setattr(cache, "purge_expired_via_writer", purge)
        # Do not actually wait an hour on the old implementation.
        monkeypatch.setattr(stop, "wait", lambda timeout: True)
        server._purge_loop(stop, writer)
        purge.assert_called_once_with(writer, block=False)


def test_stopped_broker_does_not_start_cleanup(monkeypatch):
    stop = threading.Event()
    stop.set()
    purge = Mock()
    monkeypatch.setattr(cache, "purge_expired_via_writer", purge)
    server._purge_loop(stop, object())
    purge.assert_not_called()


def test_stop_after_interval_does_not_submit_another_purge(monkeypatch):
    stop = threading.Event()
    future = Future()
    future.set_result(0)
    purge = Mock(return_value=future)
    monkeypatch.setattr(cache, "purge_expired_via_writer", purge)

    def wait(timeout):
        assert timeout == 60
        stop.set()
        # Simulate stop arriving immediately after an interval expires.
        return False

    monkeypatch.setattr(stop, "wait", wait)
    server._purge_loop(stop, object())
    purge.assert_called_once()


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("submit failed"),
        ValueError("commit failed"),
        TimeoutError("commit failed"),
    ],
)
def test_cleanup_error_is_logged_and_next_tick_runs(monkeypatch, caplog, failure):
    stop = threading.Event()
    failed = Future()
    failed.set_exception(failure)
    finished = Future()
    finished.set_result(0)
    purge = Mock(
        side_effect=[failure if isinstance(failure, RuntimeError) else failed, finished]
    )
    monkeypatch.setattr(cache, "purge_expired_via_writer", purge)
    waits = 0

    def wait(timeout):
        nonlocal waits
        assert timeout == 60
        waits += 1
        if waits == 2:
            stop.set()
        return stop.is_set()

    monkeypatch.setattr(stop, "wait", wait)
    server._purge_loop(stop, object())
    assert purge.call_count == 2
    assert "broker purge: tick failed, continuing" in caplog.text


def test_full_writer_queue_defers_until_next_interval(monkeypatch):
    stop = threading.Event()
    complete = Future()
    complete.set_result(0)
    purge = Mock(side_effect=[queue.Full, complete])
    monkeypatch.setattr(cache, "purge_expired_via_writer", purge)
    waits = Mock(side_effect=[False, True])
    monkeypatch.setattr(stop, "wait", waits)
    server._purge_loop(stop, object())
    assert purge.call_count == 2
    assert all(call.kwargs == {"block": False} for call in purge.call_args_list)
    assert [call.args for call in waits.call_args_list] == [(60,), (60,)]


def test_active_vacuum_defers_without_counting_a_skipped_rpc(monkeypatch):
    from mimir.broker.handlers.cache import begin_vacuum, end_vacuum

    stop = threading.Event()
    purge = Mock()
    monkeypatch.setattr(cache, "purge_expired_via_writer", purge)
    monkeypatch.setattr(stop, "wait", lambda timeout: True)
    begin_vacuum()
    try:
        server._purge_loop(stop, object())
        purge.assert_not_called()
    finally:
        assert end_vacuum() == 0


def test_pending_purge_is_not_resubmitted_and_stop_leaves_writer_ownership(monkeypatch):
    stop = threading.Event()
    pending = Future()
    purge = Mock(return_value=pending)
    monkeypatch.setattr(cache, "purge_expired_via_writer", purge)
    polls = 0

    def wait(timeout):
        nonlocal polls
        assert timeout == server.SHUTDOWN_POLL_SEC
        polls += 1
        assert purge.call_count == 1
        if polls == 5:
            stop.set()
        return stop.is_set()

    monkeypatch.setattr(stop, "wait", wait)
    server._purge_loop(stop, object())
    assert polls == 5
    assert not pending.done()
    assert not pending.cancelled()


def test_stop_interrupts_wait_for_pending_writer(monkeypatch):
    stop = threading.Event()
    submitted = threading.Event()
    pending = Future()

    def submit(writer, *, block):
        assert block is False
        submitted.set()
        return pending

    monkeypatch.setattr(cache, "purge_expired_via_writer", submit)
    thread = threading.Thread(
        target=server._purge_loop, args=(stop, object()), daemon=True
    )
    thread.start()
    try:
        assert submitted.wait(1)
        stop.set()
        thread.join(timeout=1)
        assert not thread.is_alive(), (
            "timer waited for an unfinished writer on shutdown"
        )
        assert not pending.done()
    finally:
        stop.set()
        pending.set_result(0)
        thread.join(timeout=1)


def test_purge_cutoff_is_fixed_before_writer_executes(monkeypatch, seeded_db):
    from sqlalchemy import insert, select

    from mimir.models import CacheEntry

    with seeded_db() as session:
        session.execute(
            insert(CacheEntry).values(
                [
                    {"key": "purge-before", "value": "null", "expires_at": 99},
                    {"key": "purge-equal", "value": "null", "expires_at": 100},
                    {"key": "purge-after", "value": "null", "expires_at": 101},
                ]
            )
        )
        session.commit()
    writer = Mock()
    monkeypatch.setattr(cache, "_now", lambda: 100)
    cache.purge_expired_via_writer(writer, block=False)
    writer.submit.assert_called_once()
    assert writer.submit.call_args.kwargs == {"block": False}
    operation = writer.submit.call_args.args[0]
    monkeypatch.setattr(cache, "_now", lambda: 200)
    with seeded_db() as session:
        operation.fn(session.connection())
        session.commit()
        remaining = set(
            session.scalars(
                select(CacheEntry.key).where(
                    CacheEntry.key.in_(["purge-before", "purge-equal", "purge-after"])
                )
            )
        )
    assert remaining == {"purge-equal", "purge-after"}
