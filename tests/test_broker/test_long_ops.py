"""End-to-end tests for the Phase 2.0 long-op family.

Covers:
- `handlers.classify_op` routes recognised ops to the cache or
  long queue correctly, unknown/malformed ops route to cache so
  `dispatch` can produce a structured failure reply.
- The broker's `bootstrap_inboxes` handler reconciles
  `Settings.inboxes` into the DB and reports the count.
- The per-op timeout override on `BrokerClient._rpc` actually
  applies during a long op and is restored afterwards (so a
  follow-up cache op uses the default 5s).
- The two-worker design lets a cache op flow even while the long
  worker is busy with a deliberately slow op.
"""
import threading
import time

from mimir.broker import handlers
from mimir.broker.client import BrokerClient
from tests.test_broker._helpers import broker_running, short_socket_path


# ----- classify_op --------------------------------------------------------


def test_classify_op_recognises_cache_ops():
    """`cache_set`, `cache_delete`, `cache_delete_for_inbox`,
    `cache_purge_expired`, and `ping` are all cache ops; they must
    NOT show up in `LONG_OPS`."""
    for op in (
        "cache_set", "cache_delete", "cache_delete_for_inbox",
        "cache_purge_expired", "ping",
    ):
        assert op not in handlers.LONG_OPS


def test_classify_op_recognises_long_ops():
    """`bootstrap_inboxes` is in `LONG_OPS`. As Phase 2.1+ migrations
    land, the rest (ingest_epoch, backfill_*, update_mainline,
    analyze, vacuum) get added here; pinning the canary now is
    cheap insurance."""
    assert "bootstrap_inboxes" in handlers.LONG_OPS


def test_classify_op_returns_op_name():
    """Reader uses this to route. Plain string return on the happy
    path."""
    assert handlers.classify_op(b'{"op": "cache_set"}') == "cache_set"
    assert handlers.classify_op(b'{"op": "bootstrap_inboxes"}') == "bootstrap_inboxes"


def test_classify_op_returns_none_on_malformed_or_unknown():
    """Malformed JSON or missing `op` returns None so the reader
    routes the line to the cache queue, and `dispatch` produces a
    structured failure reply on the regular path."""
    assert handlers.classify_op(b'not json') is None
    assert handlers.classify_op(b'{}') is None
    assert handlers.classify_op(b'[1, 2, 3]') is None  # JSON, but not a dict
    assert handlers.classify_op(b'{"op": 123}') is None  # op is not a string


# ----- bootstrap_inboxes via the broker -----------------------------------


def test_bootstrap_inboxes_via_broker_reconciles_inboxes(seeded_db):
    """End-to-end: spin a broker on a tmp socket, invoke
    `BrokerClient.bootstrap_inboxes()`, verify it returns the
    count from `mimir.inboxes.bootstrap_inboxes()` and the DB
    actually got the rows."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox

    sp = short_socket_path("bootstrap")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            count = c.bootstrap_inboxes()
        finally:
            c.close()

    # Bootstrap is idempotent on top of seeded_db (alpha/beta
    # already exist). The returned count is the number of env-
    # configured inboxes that ended up reconciled; with the default
    # `Settings.inboxes` plus the test fixture, count is the union.
    assert count >= 2  # alpha + beta at minimum from the fixture.

    # The seeded inboxes survive (idempotent semantics).
    with SessionLocal() as s:
        names = {
            row.name for row in s.execute(select(Inbox)).scalars().all()
        }
    assert "alpha" in names
    assert "beta" in names


def test_bootstrap_inboxes_via_broker_returns_count_on_reply(seeded_db):
    """The `Reply.result` field carries `{"inboxes": N}`; the
    client extracts and returns N. Lower-level pin so a future
    accidental rename of the dict key surfaces here, not via a
    silent zero return at every operator's CLI invocation."""
    sp = short_socket_path("bootstrap-count")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            # Two consecutive calls (idempotent); same count both
            # times.
            n1 = c.bootstrap_inboxes()
            n2 = c.bootstrap_inboxes()
        finally:
            c.close()
    assert n1 == n2
    assert n1 > 0


# ----- per-op timeout override --------------------------------------------


def test_per_op_timeout_restored_after_rpc(seeded_db):
    """After a long-op RPC overrides the socket timeout to its
    larger value, the client must restore the default before the
    next call. Otherwise a follow-up cache op would inherit the
    long-op timeout and a slow broker dispatch would block much
    longer than the 5s cache-op budget."""
    sp = short_socket_path("timeout-restore")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            # Force a connection by sending a ping (default timeout).
            assert c.ping() is True
            assert c._sock is not None
            default_t = c._sock.gettimeout()

            # Long op with a deliberately large timeout.
            c.bootstrap_inboxes(timeout=42.0)

            # Socket's timeout must be restored to the default
            # after the long op returns; otherwise the next cache
            # op would silently inherit the 42s budget.
            assert c._sock is not None
            assert c._sock.gettimeout() == default_t
        finally:
            c.close()


# ----- two-worker concurrency --------------------------------------------


def test_long_worker_does_not_block_cache_worker(seeded_db, monkeypatch):
    """Pin the load-bearing property of the two-worker split: while
    the long worker is in the middle of a slow op, the cache worker
    continues to process cache RPCs. Without per-queue workers, a
    long op would head-of-line block every cache.set behind it."""
    from mimir import cache
    from mimir.broker import handlers as _handlers

    # Monkey-patch the bootstrap handler to sleep so the long
    # worker is held in dispatch for ~0.5s. A real ingest would
    # take much longer; 0.5s is enough to deterministically
    # observe the cache op completing during that window.
    real_handle = _handlers._handle_bootstrap_inboxes

    def slow_handle(req):
        time.sleep(0.5)
        return real_handle(req)

    monkeypatch.setattr(_handlers, "_handle_bootstrap_inboxes", slow_handle)
    # The dispatch table holds a reference captured at import
    # time; patch that entry too so the dispatch call actually
    # routes through `slow_handle`.
    real_entry = _handlers._DISPATCH["bootstrap_inboxes"]
    _handlers._DISPATCH["bootstrap_inboxes"] = (real_entry[0], slow_handle)
    try:
        sp = short_socket_path("two-workers")
        with broker_running(sp):
            # Two separate clients: one fires the slow long op,
            # one keeps pumping cache ops while it runs.
            long_client = BrokerClient(sp)
            cache_client = BrokerClient(sp)
            try:
                done_event = threading.Event()

                def _run_long():
                    try:
                        long_client.bootstrap_inboxes(timeout=10.0)
                    finally:
                        done_event.set()

                t = threading.Thread(target=_run_long, daemon=True)
                t.start()

                # Tiny delay to let the long op land in the long
                # queue and reach the long worker.
                time.sleep(0.05)
                # If the long worker were head-of-line blocking
                # the cache worker, this would wait the full
                # 0.5s. With two workers, it returns immediately.
                t0 = time.perf_counter()
                cache_client.cache_set(
                    cache._ns("two-workers-test"), '"v"', 60,
                )
                cache_elapsed_ms = (time.perf_counter() - t0) * 1000.0
                # Generous bound: typical cache_set is single-
                # digit ms. Anything well under the 500ms long-op
                # sleep proves the cache worker wasn't blocked.
                assert cache_elapsed_ms < 200, (
                    f"cache op took {cache_elapsed_ms:.1f}ms while long "
                    f"worker was busy; expected fast (< 200ms) if the "
                    f"two-worker split is doing its job"
                )

                # Let the long op finish so the test tears down
                # cleanly.
                done_event.wait(timeout=5.0)
            finally:
                long_client.close()
                cache_client.close()
    finally:
        _handlers._DISPATCH["bootstrap_inboxes"] = real_entry
