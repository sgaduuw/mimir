"""End-to-end integration: pipelined RPCs against a real broker.

Validates the full path: client allocates rpc_ids, sends concurrent
requests, broker dispatches across multiple worker threads, each
worker writes a reply under the per-connection send_lock, client
demux thread resolves futures by rpc_id."""

import threading

import pytest

from mimir import cache as cache_mod
from mimir.broker.client import BrokerClient
from tests.test_broker._helpers import broker_running, short_socket_path


def test_sixteen_concurrent_pings_all_complete(seeded_db, monkeypatch):
    """Sixteen caller threads each issue a ping; all complete
    successfully. Broker has at least one cache worker (default).
    Pipelining means the workers' replies all land back at the
    client demux thread and resolve to the correct futures."""
    # Bump cache workers so the broker can actually drain 16 pings
    # in parallel rather than serializing them at one worker.
    from mimir.config import settings
    monkeypatch.setattr(settings, "broker_cache_workers", 4)

    sp = short_socket_path("integration-16-pings")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            results = [None] * 16

            def fire(i: int) -> None:
                results[i] = c.ping()

            threads = [threading.Thread(target=fire, args=(i,)) for i in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
            for i, t in enumerate(threads):
                assert not t.is_alive(), f"thread {i} hung"
            assert all(results), results
        finally:
            c.close()


def test_mixed_op_types_concurrent(seeded_db, monkeypatch):
    """Mix cache_set, ping across threads. Every reply demuxes to
    the correct caller. Verifies all 8 cache writes landed."""
    from mimir.config import settings
    monkeypatch.setattr(settings, "broker_cache_workers", 4)

    sp = short_socket_path("integration-mixed")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            def do_set(i: int) -> None:
                c.cache_set(cache_mod._ns(f"key_{i}"), f'"v_{i}"', 60)

            def do_ping(slot: list) -> None:
                slot.append(c.ping())

            ping_slots: list = []
            threads = []
            for i in range(8):
                threads.append(threading.Thread(target=do_set, args=(i,)))
                threads.append(threading.Thread(target=do_ping, args=(ping_slots,)))

            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
            for i, t in enumerate(threads):
                assert not t.is_alive(), f"thread {i} hung"
            # All 8 pings should have returned True.
            assert len(ping_slots) == 8
            assert all(ping_slots), ping_slots
        finally:
            c.close()

    # Verify all eight cache writes landed (post-broker; read direct).
    for i in range(8):
        assert cache_mod.get(f"key_{i}") == f"v_{i}"
