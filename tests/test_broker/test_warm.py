"""Phase 2.2 warm-queue RPCs: per-inbox + global cache warming
via the broker's third queue (N parallel warm-workers).

Covers:
- `WARM_OPS` routing classification and dispatch-table entries.
- `handle_warm_inbox` runs the per-inbox target list and reports
  `{warmed, elapsed_ms, errors}`. `targets=None` warms everything;
  a narrowed subset warms only the labelled targets.
- `handle_warm_global` runs the cross-inbox aggregators.
- Per-target exceptions are captured in `errors` rather than
  failing the whole RPC (best-effort warming).
- `BrokerClient.warm_inbox` and `warm_global` round-trip cleanly
  through a real broker on a tmp socket.
- The CLI's broker-mode dispatch fans warm_inbox jobs and a
  single warm_global, then prints the summary.
- Multi-worker drain: with `broker_warm_workers > 1`, two
  concurrent warm_inbox RPCs make progress in parallel rather
  than serialising on a single worker.
"""

from __future__ import annotations

import time

import pytest

from mimir.broker import _context, handlers
from mimir.broker.client import BrokerClient
from mimir.broker.handlers import dispatch
from mimir.broker.pools import ReadSessionPool
from mimir.broker.protocol import WarmGlobalRequest, WarmInboxRequest
from mimir.broker.writes import WriterThread
from tests.test_broker._helpers import broker_running, short_socket_path


def _line(req) -> bytes:
    return req.model_dump_json().encode("utf-8")


@pytest.fixture(autouse=True)
def _active_broker_context(seeded_db):
    """Auto-register the active broker context for warm handlers.
    This fixture ensures every test in this file has the broker
    context set up so handlers can reach the active pool + writer.
    Tear down after each test.

    Saves and restores the pre-test active context so the session-
    scoped _session_broker fixture's registration survives across
    test files. Without the restore, every test in this file would
    leave the global _context cleared, causing all subsequent tests
    that rely on the session broker (e.g. test_cli/test_cache.py
    warm-cache tests) to fail with "No active broker"."""
    saved_pool = _context._active_pool
    saved_writer = _context._active_writer
    pool = ReadSessionPool.from_settings()
    writer = WriterThread.from_settings()
    writer.start()
    try:
        _context.set_active(pool, writer)
        yield
    finally:
        writer.stop(timeout=5)
        pool.close()
        # Restore the session broker's registration.
        if saved_pool is not None and saved_writer is not None:
            _context.set_active(saved_pool, saved_writer)
        else:
            _context.clear_active()


# ----- WARM_OPS routing ---------------------------------------------------


def test_warm_ops_routing_set():
    """`warm_inbox` and `warm_global` are the only members of the
    warm routing set. Pinning so a future op accidentally added
    to LONG_OPS doesn't shadow the warm queue."""
    assert handlers.WARM_OPS == frozenset({"warm_inbox", "warm_global"})
    # No warm op shows up in LONG_OPS (mutual exclusion).
    assert not (handlers.WARM_OPS & handlers.LONG_OPS)


def test_warm_ops_in_dispatch():
    """Both warm ops must be present in the dispatch table so the
    worker can validate-and-dispatch."""
    assert "warm_inbox" in handlers._DISPATCH
    assert "warm_global" in handlers._DISPATCH


# ----- handle_warm_inbox --------------------------------------------------


def test_dispatch_warm_inbox_reply_shape(seeded_db):
    """Happy path: dispatch a warm_inbox RPC for the seeded `alpha`
    inbox, get back `{warmed, elapsed_ms, errors}`. The seeded
    fixture has enough rows that the targets run cleanly; we don't
    pin the exact `warmed` content because the helper list evolves,
    but the shape and the absence of errors is pinned."""
    reply = dispatch(_line(WarmInboxRequest(inbox_name="alpha")))
    assert reply.ok is True, reply.error
    assert reply.result is not None
    assert "warmed" in reply.result
    assert "elapsed_ms" in reply.result
    assert "errors" in reply.result
    assert isinstance(reply.result["warmed"], list)
    assert isinstance(reply.result["errors"], list)
    # Sanity: at least the core per-inbox helpers landed without
    # error on the seeded test corpus.
    assert len(reply.result["warmed"]) >= 5
    assert reply.result["errors"] == [], (
        f"unexpected warm errors: {reply.result['errors']}"
    )


def test_dispatch_warm_inbox_unknown_returns_clean_error(seeded_db):
    """Bogus inbox name returns a structured `UnknownInbox:<name>`
    error rather than crashing the handler. Mirrors the matching
    contract on `ingest_inbox`."""
    reply = dispatch(_line(WarmInboxRequest(inbox_name="does-not-exist")))
    assert reply.ok is False
    assert reply.error is not None
    assert "UnknownInbox:does-not-exist" in reply.error


def test_dispatch_warm_inbox_targets_narrows_set(seeded_db):
    """`targets=[label]` warms only the labelled subset. The post-
    ingest warm shape uses this to refresh just the front-page-
    critical helpers, not the whole per-inbox sweep."""
    reply = dispatch(
        _line(
            WarmInboxRequest(
                inbox_name="alpha",
                targets=["alpha archive_stats"],
            )
        )
    )
    assert reply.ok is True
    # Only the requested label landed.
    assert reply.result["warmed"] == ["alpha archive_stats"]


def test_dispatch_warm_inbox_emits_slow_breakdown_log(seeded_db, monkeypatch, caplog):
    """Per-target timings ride along on every reply, and when the
    handler's elapsed time crosses `broker_slow_rpc_warn_ms` a
    breakdown WARNING fires so an operator chasing a `broker slow
    rpc warm_inbox` line in journalctl can see which helpers ate
    the budget without a separate `-v` repro.
    """

    def slow(_s):
        time.sleep(0.05)

    def fast(_s):
        pass

    def fake_build(inbox, today, yesterday, sitemap_base):
        return [
            ("alpha fast", fast),
            ("alpha slow", slow),
        ]

    import mimir.cli.cache as cli_cache
    from mimir.config import settings

    monkeypatch.setattr(cli_cache, "_build_inbox_targets", fake_build)
    # Trip the breakdown log on the 50 ms sleep but stay above the
    # 0 ms no-op so the absence-on-fast-paths invariant is testable
    # by other tests in this file (which do not set a threshold and
    # rely on the production default of 100 ms not firing on the
    # tiny seeded corpus).
    monkeypatch.setattr(settings, "broker_slow_rpc_warn_ms", 10)

    caplog.set_level("WARNING", logger="mimir.broker.handlers.warm")
    reply = dispatch(_line(WarmInboxRequest(inbox_name="alpha")))
    assert reply.ok is True

    per_target = reply.result["per_target"]
    labels = [entry["label"] for entry in per_target]
    assert labels == ["alpha slow", "alpha fast"]
    assert per_target[0]["ms"] >= per_target[1]["ms"]

    slow_records = [r for r in caplog.records if "broker warm slow" in r.getMessage()]
    assert len(slow_records) == 1
    msg = slow_records[0].getMessage()
    assert "warm_inbox" in msg
    assert "alpha" in msg
    assert "alpha slow=" in msg


def test_dispatch_warm_inbox_captures_per_target_errors(seeded_db, monkeypatch):
    """A target that raises must NOT fail the whole RPC; the
    exception is captured in `errors` and the other targets keep
    going. Mirrors `_warm_after_ingest`'s best-effort posture."""

    def boom(_s):
        raise RuntimeError("synthetic warm failure")

    def fake_build(inbox, today, yesterday, sitemap_base):
        return [
            ("alpha healthy", lambda s: None),
            ("alpha boom", boom),
        ]

    import mimir.cli.cache as cli_cache

    monkeypatch.setattr(cli_cache, "_build_inbox_targets", fake_build)

    reply = dispatch(_line(WarmInboxRequest(inbox_name="alpha")))
    assert reply.ok is True
    assert reply.result["warmed"] == ["alpha healthy"]
    assert len(reply.result["errors"]) == 1
    assert "alpha boom" in reply.result["errors"][0]
    assert "synthetic warm failure" in reply.result["errors"][0]


# ----- handle_warm_global -------------------------------------------------


def test_dispatch_warm_global_reply_shape(seeded_db):
    """warm_global runs the cross-inbox aggregators
    (most_active_subsystems_global + sitemap when SITE_BASE_URL is
    set). On the seeded test corpus there's at least the one
    aggregator target."""
    reply = dispatch(_line(WarmGlobalRequest()))
    assert reply.ok is True, reply.error
    assert reply.result is not None
    assert "warmed" in reply.result
    assert "elapsed_ms" in reply.result
    assert "errors" in reply.result
    # `most_active_subsystems_global (7d)` is the always-on global
    # target; sitemap entries are gated on SITE_BASE_URL.
    assert any("most_active_subsystems_global" in w for w in reply.result["warmed"])


# ----- BrokerClient round-trip --------------------------------------------


def test_broker_client_warm_inbox_round_trip(seeded_db):
    """End-to-end: spin a real broker on a tmp socket, send a
    warm_inbox RPC, get the result dict back unchanged. Exercises
    the framing + JSONL + multi-worker drain together."""
    sp = short_socket_path("warm-inbox-rt")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            result = c.warm_inbox("alpha")
        finally:
            c.close()
    assert isinstance(result, dict)
    assert "warmed" in result and "elapsed_ms" in result and "errors" in result
    assert result["errors"] == []


def test_broker_client_warm_global_round_trip(seeded_db):
    """Same end-to-end shape on warm_global."""
    sp = short_socket_path("warm-global-rt")
    with broker_running(sp):
        c = BrokerClient(sp)
        try:
            result = c.warm_global()
        finally:
            c.close()
    assert isinstance(result, dict)
    assert any("most_active_subsystems_global" in w for w in result["warmed"])


# ----- Multi-worker concurrency ------------------------------------------


def test_warm_workers_drain_in_parallel(seeded_db, monkeypatch):
    """The load-bearing property of the warm queue: with N>1 workers,
    two warm_inbox RPCs run concurrently rather than serialising on
    a single worker. Confirms the multi-worker drain is actually
    parallel and not just one worker spinning N labels.

    Strategy: monkey-patch the warm handler to sleep 0.5 s, fire
    two concurrent warm_inbox RPCs from two clients, expect the
    pair to complete in well under 1 s (i.e. they overlapped) if
    multi-worker drain works. Single-worker would take ~1 s.
    """
    import threading
    from mimir.broker import handlers as _handlers
    from mimir.broker.handlers.warm import handle_warm_inbox

    def slow_handle(req):
        time.sleep(0.5)
        return handle_warm_inbox(req)

    real_entry = _handlers._DISPATCH["warm_inbox"]
    _handlers._DISPATCH["warm_inbox"] = (real_entry[0], slow_handle)
    try:
        sp = short_socket_path("warm-parallel")
        with broker_running(sp):
            c1 = BrokerClient(sp)
            c2 = BrokerClient(sp)
            try:
                results: list[float] = []
                lock = threading.Lock()

                def fire(client):
                    t0 = time.perf_counter()
                    client.warm_inbox("alpha")
                    with lock:
                        results.append(time.perf_counter() - t0)

                t1 = threading.Thread(target=fire, args=(c1,), daemon=True)
                t2 = threading.Thread(target=fire, args=(c2,), daemon=True)
                start = time.perf_counter()
                t1.start()
                t2.start()
                t1.join(timeout=5)
                t2.join(timeout=5)
                wall_elapsed = time.perf_counter() - start

                # With 4 warm workers (default) and two slow ops,
                # both should run in parallel; total wall time
                # should be close to 0.5 s, not 1.0 s. A generous
                # bound of 0.9 s rules out serial execution while
                # tolerating thread scheduling jitter.
                assert wall_elapsed < 0.9, (
                    f"two warm_inbox RPCs took {wall_elapsed:.2f}s "
                    f"wall-clock; expected ~0.5 s (parallel). "
                    f"Multi-worker drain may have regressed to serial."
                )
            finally:
                c1.close()
                c2.close()
    finally:
        _handlers._DISPATCH["warm_inbox"] = real_entry


# ----- CLI dispatch -------------------------------------------------------


def test_warm_cache_command_broker_mode_dispatches_via_rpc(
    seeded_db,
    monkeypatch,
):
    """`mimir warm-cache` in broker mode fans warm_inbox RPCs per
    configured inbox and exits, plus one warm_global. Asserts the
    dispatch shape end-to-end without spinning a broker: stub
    `BrokerClient.warm_inbox` / `warm_global` to capture calls.
    """
    from pathlib import Path
    from click.testing import CliRunner

    from mimir.broker import client as broker_client_mod
    from mimir.cli import warm_cache_command
    from mimir.config import settings as live_settings

    # Activate broker mode for the duration of this test. The
    # broker socket path doesn't need to be real because we patch
    # the client at the singleton accessor.
    monkeypatch.setattr(
        live_settings,
        "broker_socket_path",
        Path("/tmp/mimir-test-warm-cli.sock"),
    )

    seen_inboxes: list[str] = []
    seen_global = [0]

    class FakeClient:
        def warm_inbox(self, name, **_kw):
            seen_inboxes.append(name)
            return {"warmed": ["x"], "elapsed_ms": 1, "errors": []}

        def warm_global(self, **_kw):
            seen_global[0] += 1
            return {"warmed": ["global"], "elapsed_ms": 1, "errors": []}

        def cache_purge_expired(self):
            # Called by warm-cache at the end; stubbed so the
            # CLI's tail-end purge step doesn't blow up.
            return 0

    monkeypatch.setattr(
        broker_client_mod,
        "get_broker_client",
        lambda: FakeClient(),
    )
    # Also patch via the CLI's import name since `warm_cache_command`
    # imports `get_broker_client` inside the function body.

    result = CliRunner().invoke(warm_cache_command, ["-v"])
    assert result.exit_code == 0, result.output
    # Every configured inbox got a warm_inbox dispatch.
    assert set(seen_inboxes) >= {"alpha", "beta"}
    # warm_global fired exactly once.
    assert seen_global[0] == 1
    # Summary line still emits.
    assert "warm-cache:" in result.output and "ms total" in result.output


# ----- Pool migration (Phase 2) -------------------------------------------


def test_handle_warm_inbox_uses_active_read_pool_session(monkeypatch):
    """handle_warm_inbox should check its session out of the active
    ReadSessionPool, not SessionLocal. Verifies by monkeypatching
    SessionLocal to raise and checking the handler still works
    when the active pool is registered."""
    from mimir.broker.handlers.warm import handle_warm_inbox
    from mimir.broker.protocol import WarmInboxRequest

    def explode(*a, **kw):
        raise AssertionError("handler must use the active pool, not SessionLocal")

    # Patch the canonical source. If any code path imports SessionLocal
    # from mimir.extensions and calls it during the handler invocation,
    # the test fails loudly. This catches future regressions where
    # someone re-imports SessionLocal into warm.py.
    monkeypatch.setattr("mimir.extensions.SessionLocal", explode)

    # The _active_broker_context fixture has already set up the pool,
    # so the handler should succeed without falling back to SessionLocal.
    reply = handle_warm_inbox(WarmInboxRequest(inbox_name="alpha", targets=None))
    assert reply.ok is True


def test_handle_warm_global_uses_active_read_pool_session(monkeypatch):
    """handle_warm_global should NOT touch SessionLocal. It should
    check its read session out of the active pool (directly or via
    _run_targets, depending on the function structure). Verifies by
    monkeypatching SessionLocal to raise and checking the handler
    still works when the active pool is registered."""
    from mimir.broker.handlers.warm import handle_warm_global
    from mimir.broker.protocol import WarmGlobalRequest

    def explode(*a, **kw):
        raise AssertionError("handler must use the active pool, not SessionLocal")

    # Patch the canonical source. If any code path imports SessionLocal
    # from mimir.extensions and calls it during the handler invocation,
    # the test fails loudly. This catches future regressions where
    # someone re-imports SessionLocal into warm.py.
    monkeypatch.setattr("mimir.extensions.SessionLocal", explode)

    # The _active_broker_context fixture has already set up the pool,
    # so the handler should succeed without falling back to SessionLocal.
    reply = handle_warm_global(WarmGlobalRequest())
    assert reply.ok is True


# Task 5 of the fast/slow tier split (spec §2 §5): `_run_targets`
# selects per-tier `refresh_window` + `ttl_extension` from settings;
# the handlers pass `priority=req.priority` through. The no-
# preemption pin captures the load-bearing semantic that a slow op
# already in flight is NOT cancelled when a fast op queues behind
# it: priority is a queue-ordering policy, not a cancellation
# primitive.


def test_warm_workers_do_not_preempt_in_flight_slow_op(seeded_db, monkeypatch):
    """Load-bearing decision pin: once a slow op starts on a worker,
    a fast op queued behind it does NOT preempt it. The fast op
    waits for slow to finish, then runs. Strategy: 1 worker,
    monkey-patch warm handler to sleep 200 ms, fire one slow then
    one fast, assert slow completes before fast."""
    import threading

    from mimir.broker import handlers as _handlers
    from mimir.broker.client import BrokerClient
    from mimir.broker.handlers.warm import handle_warm_inbox
    from mimir.config import settings

    monkeypatch.setattr(settings, "broker_warm_workers", 1)

    def slow_handle(req):
        time.sleep(0.2)
        return handle_warm_inbox(req)

    real_entry = _handlers._DISPATCH["warm_inbox"]
    _handlers._DISPATCH["warm_inbox"] = (real_entry[0], slow_handle)
    try:
        sp = short_socket_path("warm-preempt")
        with broker_running(sp):
            c_slow = BrokerClient(sp)
            c_fast = BrokerClient(sp)
            try:
                completions: dict[str, float] = {}
                done = threading.Event()

                def fire_slow():
                    c_slow.warm_inbox("alpha")  # priority=1 default
                    completions["slow"] = time.perf_counter()
                    if "fast" in completions:
                        done.set()

                def fire_fast():
                    c_fast.warm_inbox("alpha", priority=0)
                    completions["fast"] = time.perf_counter()
                    if "slow" in completions:
                        done.set()

                t_slow = threading.Thread(target=fire_slow, daemon=True)
                t_fast = threading.Thread(target=fire_fast, daemon=True)
                t_slow.start()
                time.sleep(0.05)  # give slow a head start
                t_fast.start()
                done.wait(timeout=3)

                assert "slow" in completions and "fast" in completions
                assert completions["slow"] < completions["fast"], (
                    "in-flight slow op should not be preempted; got "
                    f"slow={completions['slow']:.3f}, fast={completions['fast']:.3f}"
                )
            finally:
                c_slow.close()
                c_fast.close()
    finally:
        _handlers._DISPATCH["warm_inbox"] = real_entry


def test_run_targets_applies_fast_tier_window_for_priority_zero(seeded_db, monkeypatch):
    """`_run_targets(targets, priority=0)` wraps execution in
    refresh_window(warm_cache_fast_refresh_window_sec) and
    ttl_extension(warm_cache_fast_refresh_window_sec). Verified
    by monkey-patching refresh_window + ttl_extension to record
    the value they're called with."""
    from contextlib import contextmanager

    from mimir.broker.handlers import warm as warm_module
    from mimir.config import settings

    monkeypatch.setattr(settings, "warm_cache_fast_refresh_window_sec", 555)

    captured: list[tuple[str, int | None]] = []

    @contextmanager
    def fake_refresh_window(seconds):
        captured.append(("refresh_window", seconds))
        yield

    @contextmanager
    def fake_ttl_extension(seconds):
        captured.append(("ttl_extension", seconds))
        yield

    monkeypatch.setattr(warm_module, "refresh_window", fake_refresh_window)
    monkeypatch.setattr(warm_module, "ttl_extension", fake_ttl_extension)

    # No-op target list so the for-loop exits without running anything.
    warm_module._run_targets([], priority=0)
    assert ("refresh_window", 555) in captured
    assert ("ttl_extension", 555) in captured


def test_run_targets_applies_slow_tier_window_for_priority_one(seeded_db, monkeypatch):
    """Mirror: priority=1 uses warm_cache_slow_refresh_window_sec."""
    from contextlib import contextmanager

    from mimir.broker.handlers import warm as warm_module
    from mimir.config import settings

    monkeypatch.setattr(settings, "warm_cache_slow_refresh_window_sec", 999)

    captured: list[tuple[str, int | None]] = []

    @contextmanager
    def fake_refresh_window(seconds):
        captured.append(("refresh_window", seconds))
        yield

    @contextmanager
    def fake_ttl_extension(seconds):
        captured.append(("ttl_extension", seconds))
        yield

    monkeypatch.setattr(warm_module, "refresh_window", fake_refresh_window)
    monkeypatch.setattr(warm_module, "ttl_extension", fake_ttl_extension)

    warm_module._run_targets([], priority=1)
    assert ("refresh_window", 999) in captured
    assert ("ttl_extension", 999) in captured


def test_run_targets_default_priority_is_slow(seeded_db, monkeypatch):
    """`_run_targets(targets)` without an explicit priority uses
    the slow-tier window. Back-compat for ad-hoc / test callers
    that omit the kwarg; the handlers always pass `priority=req.priority`
    so this fallback path is only exercised in isolation."""
    from contextlib import contextmanager

    from mimir.broker.handlers import warm as warm_module
    from mimir.config import settings

    monkeypatch.setattr(settings, "warm_cache_slow_refresh_window_sec", 1234)

    captured: list[tuple[str, int | None]] = []

    @contextmanager
    def fake_refresh_window(seconds):
        captured.append(("refresh_window", seconds))
        yield

    @contextmanager
    def fake_ttl_extension(seconds):
        captured.append(("ttl_extension", seconds))
        yield

    monkeypatch.setattr(warm_module, "refresh_window", fake_refresh_window)
    monkeypatch.setattr(warm_module, "ttl_extension", fake_ttl_extension)

    warm_module._run_targets([])
    assert ("refresh_window", 1234) in captured
    assert ("ttl_extension", 1234) in captured


# ----- Config-drift Layer 2: sitemap labels with SITE_BASE_URL unset -------
#
# When the scheduler routes sitemap labels into a broker whose
# SITE_BASE_URL is empty, the handler builds a target list that
# drops sitemap entries silently (`_build_global_targets("")` and
# `_build_fast_global_targets("")` return [] when the base is unset).
# The handler emits a once-per-process WARNING so the operator sees
# the drift in `podman logs mimir-broker`. The flag resets on broker
# restart, so a persistent misconfig fires the warning once per
# boot.


def test_warm_global_logs_sitemap_gap_warning_when_site_base_unset(
    seeded_db, caplog, monkeypatch
):
    """warm_global with sitemap labels in req.targets but
    SITE_BASE_URL empty triggers a one-line WARNING from the
    warm handler module."""
    import logging as _logging

    from mimir.broker.handlers import warm as warm_module
    from mimir.config import settings as _settings

    monkeypatch.setattr(_settings, "site_base_url", "")
    monkeypatch.setattr(warm_module, "_SITEMAP_GAP_LOGGED", False)
    caplog.set_level(_logging.WARNING, logger="mimir.broker.handlers.warm")

    reply = warm_module.handle_warm_global(
        WarmGlobalRequest(targets=["sitemap:index"], priority=0)
    )
    assert reply.ok is True

    gap_warnings = [
        r
        for r in caplog.records
        if r.levelno == _logging.WARNING
        and "sitemap labels requested" in r.getMessage()
    ]
    assert gap_warnings, (
        "expected a sitemap-gap WARNING; got "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    msg = gap_warnings[0].getMessage()
    assert "sitemap:index" in msg
    assert "SITE_BASE_URL" in msg


def test_warm_global_sitemap_gap_warning_fires_once_per_process(
    seeded_db, caplog, monkeypatch
):
    """The WARNING is gated on a module-level flag so 200 warm RPCs
    per scheduler tick do not flood the log. A second call with the
    same misconfig logs nothing."""
    import logging as _logging

    from mimir.broker.handlers import warm as warm_module
    from mimir.config import settings as _settings

    monkeypatch.setattr(_settings, "site_base_url", "")
    monkeypatch.setattr(warm_module, "_SITEMAP_GAP_LOGGED", False)
    caplog.set_level(_logging.WARNING, logger="mimir.broker.handlers.warm")

    # First call: WARNING fires.
    warm_module.handle_warm_global(
        WarmGlobalRequest(targets=["sitemap:index"], priority=0)
    )
    first_count = sum(
        1 for r in caplog.records if "sitemap labels requested" in r.getMessage()
    )
    assert first_count == 1

    # Second call with the same args: no NEW WARNING.
    warm_module.handle_warm_global(
        WarmGlobalRequest(targets=["sitemap:index"], priority=0)
    )
    second_count = sum(
        1 for r in caplog.records if "sitemap labels requested" in r.getMessage()
    )
    assert second_count == 1, "second RPC should not log a second sitemap-gap WARNING"


def test_warm_global_no_gap_warning_when_site_base_set(seeded_db, caplog, monkeypatch):
    """Happy path: SITE_BASE_URL set, sitemap targets requested, no
    gap WARNING. The runtime path runs the targets normally."""
    import logging as _logging

    from mimir.broker.handlers import warm as warm_module
    from mimir.config import settings as _settings

    monkeypatch.setattr(_settings, "site_base_url", "https://example.com")
    monkeypatch.setattr(warm_module, "_SITEMAP_GAP_LOGGED", False)
    caplog.set_level(_logging.WARNING, logger="mimir.broker.handlers.warm")

    warm_module.handle_warm_global(
        WarmGlobalRequest(targets=["sitemap:index"], priority=0)
    )
    gap_warnings = [
        r for r in caplog.records if "sitemap labels requested" in r.getMessage()
    ]
    assert not gap_warnings


def test_warm_global_no_gap_warning_when_targets_none(seeded_db, caplog, monkeypatch):
    """targets=None means "all targets"; the local target builder
    skips sitemap entries silently when SITE_BASE_URL is unset, and
    the count discrepancy is implicit. No per-RPC WARNING in that
    case; the broker startup WARNING (Layer 1) already named the
    affected feature."""
    import logging as _logging

    from mimir.broker.handlers import warm as warm_module
    from mimir.config import settings as _settings

    monkeypatch.setattr(_settings, "site_base_url", "")
    monkeypatch.setattr(warm_module, "_SITEMAP_GAP_LOGGED", False)
    caplog.set_level(_logging.WARNING, logger="mimir.broker.handlers.warm")

    warm_module.handle_warm_global(WarmGlobalRequest(targets=None, priority=1))
    gap_warnings = [
        r for r in caplog.records if "sitemap labels requested" in r.getMessage()
    ]
    assert not gap_warnings


def test_warm_inbox_logs_sitemap_gap_warning_when_site_base_unset(
    seeded_db, caplog, monkeypatch
):
    """Same gating applies to handle_warm_inbox: sitemap labels in
    req.targets with SITE_BASE_URL unset fires the once-per-process
    WARNING."""
    import logging as _logging

    from mimir.broker.handlers import warm as warm_module
    from mimir.broker.protocol import WarmInboxRequest
    from mimir.config import settings as _settings

    monkeypatch.setattr(_settings, "site_base_url", "")
    monkeypatch.setattr(warm_module, "_SITEMAP_GAP_LOGGED", False)
    caplog.set_level(_logging.WARNING, logger="mimir.broker.handlers.warm")

    reply = warm_module.handle_warm_inbox(
        WarmInboxRequest(
            inbox_name="alpha",
            targets=["sitemap:inbox:alpha"],
            priority=0,
        )
    )
    assert reply.ok is True

    gap_warnings = [
        r for r in caplog.records if "sitemap labels requested" in r.getMessage()
    ]
    assert gap_warnings, "expected a sitemap-gap WARNING from handle_warm_inbox"
    assert "sitemap:inbox:alpha" in gap_warnings[0].getMessage()
