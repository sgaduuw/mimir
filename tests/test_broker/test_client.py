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


def test_client_reconnects_lazily_after_broker_restart(seeded_db):
    """3.0.0 fail-fast contract: after a broker restart, the next
    RPC reconnects transparently. Any RPC mid-flight at the moment
    of EOF raises BrokerUnavailable; callers handle that at their
    layer (cache.set logs and swallows)."""
    sp = short_socket_path("client-reconnect")
    c = BrokerClient(sp)
    try:
        with broker_running(sp):
            assert c.ping() is True
        # Broker is gone; the client's socket is stale.
        # The next RPC against a stopped broker raises.
        with pytest.raises(BrokerUnavailable):
            c.ping()
        # New broker on the same path. Next RPC reconnects.
        with broker_running(sp):
            assert c.ping() is True
    finally:
        try:
            if c._sock is not None:
                c._sock.close()
        except Exception:
            pass


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


def test_client_concurrent_rpcs_from_one_singleton(seeded_db):
    """Regression: warm-cache fans out across `min(cpu_count, 8)`
    worker threads, each sharing the process-singleton broker
    client. Without a per-client lock around `_rpc`, concurrent
    threads would race on the same socket: interleaved writes
    break JSONL framing, the broker returns parse errors, the
    client closes the socket, and every thread piles into a fresh
    connect attempt against the broker's listen backlog (the
    `Errno 11 Resource temporarily unavailable` connect storm
    observed in production v1.32.1 with broker mode on).

    Pin: N threads each issue M unique cache_set RPCs through a
    shared client. All complete without raising. Every key lands
    in the DB exactly once (idempotent upsert via _direct_set).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from mimir.cache import _ns

    sp = short_socket_path("client-concurrent")
    n_threads = 8
    rpcs_per_thread = 20

    with broker_running(sp):
        c = BrokerClient(sp)
        try:

            def _worker(tid: int) -> None:
                for i in range(rpcs_per_thread):
                    key = _ns(f"concurrent:{tid}:{i}")
                    c.cache_set(key, f'"v-{tid}-{i}"', 60)

            with ThreadPoolExecutor(max_workers=n_threads) as ex:
                futures = [ex.submit(_worker, t) for t in range(n_threads)]
                for f in as_completed(futures):
                    f.result()  # re-raise any worker exception
        finally:
            c.close()

    # Every key should be present exactly once.
    for tid in range(n_threads):
        for i in range(rpcs_per_thread):
            assert cache.get(f"concurrent:{tid}:{i}") == f"v-{tid}-{i}", (
                f"missing or corrupted cache row for tid={tid} i={i}"
            )


def test_client_cache_set_handles_payload_larger_than_socket_buffer(seeded_db):
    """Regression: 1.33.0 production deploy hit
    `broker: malformed JSON: Unterminated string` on sitemap
    cache_set writes (multi-KB XML). Root cause: the client used
    `makefile('wb', buffering=0).write(...)`, which delegates to
    `SocketIO.write`, which does a single `send()` and returns the
    number of bytes actually written. For payloads larger than the
    kernel socket-send buffer (~208 KB on Linux by default), the
    short-write count was ignored and the leftover bytes silently
    dropped. Broker received a truncated message and the JSON
    parser reported `Unterminated string`. Fixed by switching to
    `socket.sendall`, which loops on partial sends.

    Pin: round-trip a >1 MB value through cache_set and assert it
    lands intact. Without the fix this fails with
    `BrokerUnavailable: cache_set: MalformedJSON`.
    """
    from mimir import cache
    from mimir.broker.client import BrokerClient

    sp = short_socket_path("large-payload")
    # >1 MB of JSON-encoded content. Sitemap XML payloads on the
    # production deploy are in this range for popular inboxes.
    big = "x" * (2 * 1024 * 1024)
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            c.cache_set(cache._ns("large-payload-test"), f'"{big}"', 60)
        finally:
            c.close()
    # Round-trip: cache.get decodes the JSON. The stored value is
    # the string, so `cache.get` returns the same big string.
    got = cache.get("large-payload-test")
    assert got == big, (
        f"large-payload round-trip failed; expected {len(big)} chars, "
        f"got {len(got) if got else 'None'}"
    )


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


def test_close_fails_pending_futures(seeded_db):
    """Closing the client while futures are pending must fail them
    (not leak). Pins the shutdown contract."""
    from concurrent.futures import Future

    sp = short_socket_path("client-close")
    with broker_running(sp):
        c = BrokerClient(sp)
        c._ensure_alive()
        # Plant a fake pending future (no real RPC sent; we are
        # simulating "broker died after we registered").
        fake = Future()
        with c._pending_lock:
            c._pending[99999] = fake
        c.close()
        # Future must be resolved with BrokerUnavailable.
        assert fake.done()
        assert isinstance(fake.exception(), BrokerUnavailable)
