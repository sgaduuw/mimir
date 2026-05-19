"""Tests for `mimir.broker.client.BrokerClient`. In-thread broker
on a tmp socket; client connects, sends RPCs, asserts replies and
DB state. Reconnect behaviour is exercised by stopping the broker
mid-flight."""

import pytest

from mimir import cache
from mimir.broker.client import BrokerClient, BrokerUnavailable
from tests.test_broker._helpers import broker_running, short_socket_path


def test_client_ping_returns_true(seeded_db):
    sp = short_socket_path("client-ping")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            assert c.ping() is True
        finally:
            c.close()


def test_client_cache_set_persists_through_broker(seeded_db):
    sp = short_socket_path("client-set")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            c.cache_set(cache._ns("test_client_set"), '"hello"', 60)
        finally:
            c.close()
    # Outside the broker context, read direct.
    assert cache.get("test_client_set") == "hello"


def test_client_cache_delete_for_inbox_returns_count(seeded_db):
    cache.set("archive_stats:alpha", "x", ttl=60)
    cache.set("daily_volume:alpha:30", "y", ttl=60)

    sp = short_socket_path("client-del-inbox")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            n = c.cache_delete_for_inbox("alpha")
        finally:
            c.close()
    assert n == 2
    assert cache.get("archive_stats:alpha") is None


def test_client_raises_broker_unavailable_when_no_broker(seeded_db):
    """No broker running on the path → connect() fails → wrapped
    in `BrokerUnavailable`. Callers (in `mimir.cache`) catch this
    and log+drop, matching today's best-effort cache.set
    semantics."""
    sp = short_socket_path("nobroker")
    c = BrokerClient(sp)
    with pytest.raises(BrokerUnavailable):
        c.cache_set("x", '"v"', 60)


def test_client_reconnects_after_broker_restart(seeded_db):
    """First RPC succeeds. Stop the broker. Start a new broker on
    the same socket. Second RPC should reconnect transparently and
    succeed. Exercises the `_rpc` retry loop."""
    sp = short_socket_path("client-reconnect")
    c = BrokerClient(sp)
    try:
        with broker_running(sp):
            assert c.ping() is True
        # Broker is gone; the client's socket is stale.
        # Second broker on the same path:
        with broker_running(sp):
            # First call after restart: client reconnects on retry.
            assert c.ping() is True
    finally:
        c.close()


def test_client_persistent_connection_survives_idle_window(seeded_db):
    """Regression: production broker crashed with
    `OSError: cannot read from timed out object` after the very
    first idle window > 100ms (the handler's selector poll interval).
    Root cause: the prior server-side handler used
    `socket.settimeout` + `socket.makefile`'s SocketIO, which sets a
    permanent `_timeout_occurred` flag on the first timeout and
    every subsequent read raises OSError. Switched to
    `selectors.select` + raw `recv`; pins the contract that a
    persistent client surviving > 100ms between requests must still
    work cleanly on the next RPC."""
    import time

    sp = short_socket_path("client-idle")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            assert c.ping() is True
            # Longer than the handler's selector timeout (100ms)
            # plus a margin so we cross at least one poll boundary
            # mid-idle. The prior bug surfaced on the very NEXT
            # read after the timeout, so we just need > 100ms.
            time.sleep(0.25)
            # Without the fix this raises BrokerUnavailable on the
            # broker's broken-handler reply (or, in some races, just
            # disconnects and the client reconnects on retry, which
            # still costs latency). With the fix the broker is
            # quietly waiting and the RPC returns clean.
            assert c.ping() is True
        finally:
            c.close()


def test_client_persistent_connection_reuses_socket(seeded_db):
    """Multiple RPCs through one client reuse the same underlying
    socket (no `_close` between calls)."""
    sp = short_socket_path("client-persistent")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            assert c.ping() is True
            sock_id = id(c._sock)
            assert c.ping() is True
            assert id(c._sock) == sock_id, (
                "client opened a new socket for the second RPC; "
                "persistent-connection contract broken"
            )
        finally:
            c.close()
