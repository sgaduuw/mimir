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


def test_two_threads_dispatch_concurrently_no_serialization(seeded_db, monkeypatch):
    """REGRESSION GUARD (3.0.0): two caller threads issue RPCs
    concurrently. The first thread's RPC takes a long time on the
    broker side; the second thread's RPC must dispatch and complete
    BEFORE the first thread's reply arrives. If this test fails,
    the client has silently re-introduced a global lock that
    serialises the wire.

    Mechanism: monkeypatch the broker's _DISPATCH so the `ping` op
    routes to a slow-for-rpc_id-1 handler, and bump
    `broker_cache_workers` to 2 so both pings can dispatch in
    parallel. Thread A wins rpc_id=1 (started first), Thread B gets
    rpc_id=2. The barrier is set only when Thread B completes; if B
    is blocked behind A on a global client-side lock, the barrier
    never trips and the test times out.

    Note: `broker_cache_workers` must be >= 2 for the broker side to
    drain both pings concurrently. The default is 1 (FIFO commit
    order), which is correct for production; this test intentionally
    bumps it to 2 to exercise the CLIENT-side pipelining property in
    isolation from server-side serialisation."""
    import threading
    import time
    from mimir.broker import handlers
    from mimir.broker.protocol import PingRequest, Reply
    from mimir.config import settings

    barrier = threading.Event()

    def slow_for_rpc_id_one(req):
        if req.rpc_id == 1:
            # Wait for Thread B to complete first. If a global lock
            # were back on the client, B is queued behind A and never
            # sets the barrier -> we time out.
            barrier.wait(timeout=5.0)
        return Reply(rpc_id=req.rpc_id, ok=True)

    # Two cache workers so both pings can run in parallel on the
    # broker side. Without this the broker serialises them through
    # the single default worker, which would make the barrier test
    # false-positive (B blocks on the server queue, not on a client
    # lock). The regression we guard is client-side, so we must
    # eliminate server-side serialisation as a confound.
    monkeypatch.setattr(settings, "broker_cache_workers", 2)

    # Patch the _DISPATCH entry for "ping" so the slow handler
    # is the one the broker invokes. The PingRequest type stays
    # the same; only the handler changes.
    monkeypatch.setitem(
        handlers._DISPATCH,
        "ping",
        (PingRequest, slow_for_rpc_id_one),
    )

    sp = short_socket_path("client-regression-pipelining")
    with broker_running(sp):
        c = BrokerClient(sp)
        # Force monotonic counter to a known start so Thread A
        # gets rpc_id=1 reliably.
        c._next_rpc_id = 1
        try:
            results = {}

            def thread_a():
                # Will get rpc_id=1; handler blocks on barrier.
                results["a"] = c.ping()

            def thread_b():
                # Will get rpc_id=2; must dispatch fully even while
                # Thread A is parked on the barrier.
                results["b"] = c.ping()
                # Releasing the barrier lets Thread A finish.
                barrier.set()

            ta = threading.Thread(target=thread_a)
            tb = threading.Thread(target=thread_b)
            ta.start()
            # Tiny delay so Thread A wins the rpc_id allocation race.
            time.sleep(0.05)
            tb.start()

            ta.join(timeout=10.0)
            tb.join(timeout=10.0)
            assert not ta.is_alive(), "thread A stuck (serialization regression?)"
            assert not tb.is_alive(), "thread B stuck (serialization regression?)"
            assert results.get("a") is True
            assert results.get("b") is True
        finally:
            c.close()


def test_rpc_id_unique_under_contention(seeded_db):
    """1000 RPC id allocations across 16 threads: every allocated
    rpc_id is unique. Monotonicity is an implementation detail of
    the atomic counter, not a contract; only uniqueness is asserted."""
    import threading

    sp = short_socket_path("client-id-unique")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            seen: set[int] = set()
            seen_lock = threading.Lock()

            def fire(n: int) -> None:
                for _ in range(n):
                    rpc_id = c._allocate_rpc_id()
                    with seen_lock:
                        assert rpc_id not in seen
                        seen.add(rpc_id)

            threads = [threading.Thread(target=fire, args=(62,)) for _ in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)
            assert len(seen) == 16 * 62
        finally:
            c.close()


def test_caller_timeout_leaves_client_usable(seeded_db, monkeypatch):
    """When a caller times out on _rpc(timeout=N), the late-arriving
    reply is silently dropped by the demux thread. The client must
    stay usable for subsequent RPCs."""
    import time
    from mimir.broker import handlers
    from mimir.broker.protocol import PingRequest, Reply

    def slow_ping(req):
        time.sleep(0.5)
        return Reply(rpc_id=req.rpc_id, ok=True)

    monkeypatch.setitem(
        handlers._DISPATCH,
        "ping",
        (PingRequest, slow_ping),
    )

    sp = short_socket_path("client-timeout")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            # Force 100ms timeout; the broker sleeps 500ms.
            req = PingRequest(rpc_id=0)
            with pytest.raises(BrokerUnavailable):
                c._rpc(req, timeout=0.1)

            # Wait for the dropped reply to land in the demux thread
            # and be silently discarded.
            time.sleep(1.0)

            # Subsequent RPC works normally.
            assert c.ping() is True
        finally:
            c.close()


def test_socket_eof_fails_all_pending(seeded_db):
    """When the broker closes the socket mid-pipeline, every
    pending future receives BrokerUnavailable from the demux
    thread's fail-all step."""
    from concurrent.futures import Future

    sp = short_socket_path("client-eof")
    with broker_running(sp) as server:
        c = BrokerClient(sp)
        try:
            c._ensure_alive()
            # Plant two fake pending futures (no real RPC sent;
            # simulating "broker died after we registered").
            fake_a: Future = Future()
            fake_b: Future = Future()
            with c._pending_lock:
                c._pending[100001] = fake_a
                c._pending[100002] = fake_b
            # Force EOF: signal the broker to shut down. The demux
            # thread sees the socket close when the broker exits.
            server.stop_event.set()
            # Wait for fail-all to fire. Give the demux thread a
            # generous window; it is poll-based on readline.
            try:
                fake_a.exception(timeout=5.0)
            except TimeoutError:
                pytest.fail("demux thread did not fail-all within 5s")
            assert isinstance(fake_a.exception(), BrokerUnavailable)
            assert isinstance(fake_b.exception(), BrokerUnavailable)
        finally:
            try:
                c.close()
            except Exception:
                pass
