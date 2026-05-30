"""Tests for mimir/ingest/orchestrate.py: `discover_epochs`
(git-repo enumeration), `ingest_inbox` (per-inbox driver,
including the post-ingest auto-ANALYZE), and `ingest_all`
(across-all-inboxes driver with the shared `--limit`
budget). Also covers the multi-worker path and the
parser-failure-mid-batch ordering guarantee."""

from sqlalchemy import select

from mimir.ingest import (
    discover_epochs,
    ingest_all,
    ingest_inbox,
)
from mimir.models import (
    Inbox,
)

from tests.test_ingest._helpers import (
    _build_pubinbox_repo,
    _cache_set_direct,
    _drain_writer,
    _rfc5322,
    _setup_alpha_with_messages,
)


def test_ingest_inbox_runs_analyze_when_threshold_reached(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    from mimir.config import settings

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    monkeypatch.setattr(settings, "analyze_after_ingest_rows", 2)

    # Phase 3b: ANALYZE is dispatched via _submit_analyze imported into
    # orchestrate.py's namespace. Spy at the orchestrate module level so
    # the monkeypatch intercepts the call site (not just the source module).
    analyze_calls = []
    import mimir.ingest.orchestrate as orchestrate_mod
    from mimir.ingest._pending import _submit_analyze as real_submit_analyze

    def _spy_analyze(writer, inbox_name):
        analyze_calls.append(inbox_name)
        return real_submit_analyze(writer, inbox_name)

    monkeypatch.setattr(orchestrate_mod, "_submit_analyze", _spy_analyze)

    results = ingest_inbox(alpha, workers=1)

    # Three messages, all fresh -- exact count is known. The earlier
    # `>= 2` lower bound would have masked a regression that
    # accidentally dropped one of the three to dup_batch / failed.
    assert sum(r.new + r.linked for r in results) == 3
    assert analyze_calls, "ANALYZE should have been dispatched via _submit_analyze"


def test_ingest_inbox_skips_analyze_below_threshold(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    from mimir.config import settings
    from concurrent.futures import Future

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    monkeypatch.setattr(settings, "analyze_after_ingest_rows", 100)

    analyze_calls = []
    import mimir.ingest.orchestrate as orchestrate_mod

    def _stub_analyze(writer, inbox_name):
        analyze_calls.append(inbox_name)
        f = Future()
        f.set_result(None)
        return f

    monkeypatch.setattr(orchestrate_mod, "_submit_analyze", _stub_analyze)

    ingest_inbox(alpha, workers=1)

    assert not analyze_calls, "ANALYZE must not run below threshold"


def test_ingest_inbox_skips_analyze_when_disabled(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    from mimir.config import settings
    from concurrent.futures import Future

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    monkeypatch.setattr(settings, "analyze_after_ingest_rows", 0)

    analyze_calls = []
    import mimir.ingest.orchestrate as orchestrate_mod

    def _stub_analyze(writer, inbox_name):
        analyze_calls.append(inbox_name)
        f = Future()
        f.set_result(None)
        return f

    monkeypatch.setattr(orchestrate_mod, "_submit_analyze", _stub_analyze)

    ingest_inbox(alpha, workers=1)

    assert not analyze_calls, "ANALYZE must not run when disabled (threshold=0)"


# Cache invalidation on empty-to-non-empty transition


def test_ingest_inbox_invalidates_cache_on_first_ingest(
    seeded_db, tmp_path, broker_active
):
    """When an inbox transitions from empty (`last_article_date IS
    NULL`) to populated: (1) `delete_for_inbox` drops the stale
    `total=0` row a warm-cache tick may have left behind between
    `admin inbox add` and first ingest, and (2) the post-ingest lazy
    warm immediately rebuilds it with the real `ArchiveStats` payload.
    Net effect: the sentinel does not survive, and the cache holds a
    truthful payload before the next request hits `/`."""
    from mimir import cache

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    # The seeded `alpha` fixture stamps `last_article_date` as a
    # mid-2024 timestamp to mimic an already-ingested inbox; null it
    # back out to simulate a brand-new inbox (the actual case the
    # invalidation protects against, where a warm-cache tick wrote
    # `total=0` between `admin inbox add` and the first ingest).
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.last_article_date = None
        s.commit()

    # Seed a cache row keyed under the inbox name (mimics the `total=0`
    # row a warm-cache tick would write between add and first ingest).
    # Use _cache_set_direct so the write is immediately visible to
    # cache.get() without waiting for the WriterThread queue to drain
    # (cache.set() is fire-and-forget async when broker_active is up).
    _cache_set_direct(f"archive_stats:{alpha.name}", "sentinel", 86400)
    assert cache.get(f"archive_stats:{alpha.name}") == "sentinel"

    ingest_inbox(alpha, workers=1)
    # The post-ingest warm dispatches cache writes via set_via_writer
    # (fire-and-forget). Drain the WriterThread queue before asserting
    # the rebuilt cache state so the warm's writes have committed.
    _drain_writer()

    rebuilt = cache.get(f"archive_stats:{alpha.name}")
    assert rebuilt is not None and rebuilt != "sentinel", (
        "first ingest should drop the sentinel and the post-ingest "
        "warm should rebuild the cache row with a real payload"
    )


def test_ingest_inbox_warms_per_inbox_cache_when_missing(
    seeded_db,
    tmp_path,
    broker_active,
):
    """When the per-inbox cache rows are missing at the end of a
    moved>0 ingest tick, the post-ingest lazy warm should populate
    them. Recovers from warm-cache lag without the next request
    paying the cold-miss recompute inline."""
    from mimir import cache

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)

    # Force-evict any rows a previous fixture / helper might have
    # warmed so we're testing the cache-empty path explicitly.
    cache.delete(f"archive_stats:{alpha.name}")
    cache.delete(f"daily_volume:{alpha.name}:30")
    assert cache.get(f"archive_stats:{alpha.name}") is None
    assert cache.get(f"daily_volume:{alpha.name}:30") is None

    ingest_inbox(alpha, workers=1)
    # Post-ingest warm writes are fire-and-forget via set_via_writer.
    # Drain before asserting cache state.
    _drain_writer()

    assert cache.get(f"archive_stats:{alpha.name}") is not None, (
        "post-ingest warm should have populated the missing archive_stats cache row"
    )
    assert cache.get(f"daily_volume:{alpha.name}:30") is not None, (
        "post-ingest warm should have populated the missing daily_volume cache row"
    )


def test_ingest_inbox_warm_preserves_existing_cache_row(
    seeded_db,
    tmp_path,
    broker_active,
):
    """When the per-inbox cache row already exists at the end of a
    moved>0 ingest tick, the post-ingest warm should leave it
    untouched. force=False semantics: cache hit returns cached value,
    no recompute, no write. Keeps the 24h `archive_stats` TTL
    property intact, a steady-state UPDATE_EVERY=300s tick does not
    pay the multi-second COUNT(*) on every fire."""
    from mimir import cache

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)

    # Use _cache_set_direct so the sentinel is immediately readable
    # (cache.set is async via WriterThread when broker_active is up).
    _cache_set_direct(f"archive_stats:{alpha.name}", "sentinel", 86400)
    assert cache.get(f"archive_stats:{alpha.name}") == "sentinel"

    ingest_inbox(alpha, workers=1)
    # Post-ingest warm finds the fresh cache row (force=False) and
    # returns it without writing. Drain the writer to ensure no
    # stale queue op overwrites the sentinel.
    _drain_writer()

    assert cache.get(f"archive_stats:{alpha.name}") == "sentinel", (
        "post-ingest warm with a fresh cache row present must not "
        "overwrite it; force=False is the load-bearing property"
    )


def test_ingest_inbox_does_not_warm_on_noop_tick(seeded_db, tmp_path, broker_active):
    """Re-ingesting an already-fully-ingested inbox (every commit
    already dedup_db'd) is a no-op tick: `moved == 0`. The post-ingest
    warm must not fire on those, otherwise an inbox whose cache row
    happens to be evicted between ticks would pay an unnecessary
    cold-miss compute on every UPDATE_EVERY tick even with no new
    articles. Skip on moved==0; let warm-cache cover that case."""
    from mimir import cache

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    # First ingest moves all 3 articles in.
    ingest_inbox(alpha, workers=1)
    # Drain so the post-ingest warm's writes have committed before we
    # delete the cache row (otherwise the delete could race a still-
    # in-flight warm write that arrives after the delete and re-populates
    # it before the second ingest runs).
    _drain_writer()

    # Force-evict, then verify a second (no-op) ingest doesn't
    # re-populate.
    cache.delete(f"archive_stats:{alpha.name}")
    assert cache.get(f"archive_stats:{alpha.name}") is None

    # Second ingest against the same mirror: every commit is now
    # dedup_db, moved == 0.
    ingest_inbox(alpha, workers=1)

    assert cache.get(f"archive_stats:{alpha.name}") is None, (
        "no-op tick (moved == 0) must not trigger the post-ingest warm"
    )


def test_ingest_inbox_warm_failure_does_not_break_ingest(
    seeded_db,
    tmp_path,
    monkeypatch,
    broker_active,
):
    """The post-ingest warm is best-effort: a failure in any of its
    helper calls is logged at warning and swallowed, so an unrelated
    bug in `archive_stats` / `daily_volume` / `most_active_subsystems_in_inbox`
    cannot crash the ingest pipeline. The article rows themselves are
    already committed by the time the warm runs, so a failed warm
    just leaves the next request to recompute."""
    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated archive_stats failure")

    monkeypatch.setattr("mimir.ingest.orchestrate.archive_stats", _boom)

    # Must not raise.
    results = ingest_inbox(alpha, workers=1)
    assert sum(r.new + r.linked for r in results) == 3


def test_ingest_inbox_does_not_invalidate_cache_on_steady_state(
    seeded_db,
    tmp_path,
    broker_active,
):
    """An already-populated inbox (last_article_date set) running its
    next ingest does NOT bust the per-inbox cache. Doing so would
    force a COUNT(*) refresh on every UPDATE_EVERY tick, defeating
    the 24h TTL that exists precisely because the count is the slow
    piece."""
    from datetime import datetime, timezone

    from mimir import cache

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    # Stamp the inbox as already-populated so the empty-state
    # capture sees a non-NULL value going into ingest.
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.last_article_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        s.commit()

    # Use _cache_set_direct so the sentinel is immediately readable
    # (cache.set is async via WriterThread when broker_active is up).
    _cache_set_direct(f"archive_stats:{alpha.name}", "sentinel", 86400)
    assert cache.get(f"archive_stats:{alpha.name}") == "sentinel"

    ingest_inbox(alpha, workers=1)
    # Post-ingest warm finds the fresh cache row (force=False) and
    # returns early without writing. Drain the writer to confirm no
    # stale queued op can overwrite the sentinel.
    _drain_writer()

    assert cache.get(f"archive_stats:{alpha.name}") == "sentinel", (
        "steady-state ingest should leave the per-inbox cache alone; "
        "the 24h TTL is intentional"
    )


# Canonical inbox + list-address observation


def test_discover_epochs_returns_git_repos_only(tmp_path):
    """`discover_epochs` walks the mirror dir and returns just the
    children that look like git repos. Non-repo directories and
    regular files must be filtered -- without this, a stray
    `Inboxes/<name>/git/README` or `metadata/` would crash the
    walker downstream."""
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _build_pubinbox_repo(mirror / "0.git", [_rfc5322("e0@example.com")])
    _build_pubinbox_repo(mirror / "1.git", [_rfc5322("e1@example.com")])
    # Non-repo directory.
    (mirror / "garbage").mkdir()
    (mirror / "garbage" / "stray-file").write_text("x")
    # Stray file at the top.
    (mirror / "README").write_text("operator notes")

    epochs = discover_epochs(mirror)
    names = [p.name for p in epochs]
    assert names == ["0.git", "1.git"], (
        f"discover_epochs must skip non-repo siblings; got {names}"
    )


def test_discover_epochs_empty_mirror_returns_empty(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    assert discover_epochs(mirror) == []


def test_ingest_all_limit_decrements_across_inboxes(seeded_db, tmp_path, broker_active):
    """`ingest_all(limit=N)` is a cross-inbox cap. With two inboxes
    each carrying 3 messages and `limit=2`, the first inbox should
    consume both slots and the second must be skipped entirely --
    no ingest call, no result row. Without this contract, a
    `limit=500` across 5 inboxes could quietly ingest 2500 messages."""
    alpha_mirror = tmp_path / "alpha"
    alpha_mirror.mkdir()
    _build_pubinbox_repo(
        alpha_mirror / "0.git",
        [_rfc5322(f"alpha-cap-{i}@example.com") for i in range(3)],
    )
    beta_mirror = tmp_path / "beta"
    beta_mirror.mkdir()
    _build_pubinbox_repo(
        beta_mirror / "0.git", [_rfc5322(f"beta-cap-{i}@example.com") for i in range(3)]
    )

    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        alpha.mirror_path = str(alpha_mirror)
        beta.mirror_path = str(beta_mirror)
        s.commit()
        # Re-fetch detached copies for ingest_all's dict.
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()

    out = ingest_all(inboxes={"alpha": alpha, "beta": beta}, limit=2, workers=1)

    # alpha got an ingest result; beta did not (skipped before its
    # ingest_inbox was even called).
    assert "alpha" in out
    assert "beta" not in out, (
        f"limit should have stopped before beta's ingest; got results for "
        f"{list(out.keys())}"
    )
    # And the per-result bookkeeping: alpha's result respects the
    # cap. Either dup_batch + new + linked == 2 (the cap), or new
    # rolled through up to 3 if the per-epoch loop is more permissive
    # -- but the *cross-inbox* cap is the contract being tested.
    alpha_total = sum(
        r.new + r.linked + r.dup_batch + r.dup_db + r.failed for r in out["alpha"]
    )
    # alpha consumed at least the cap-worth; beta must not have run.
    assert alpha_total >= 2


def test_ingest_all_no_limit_walks_all_inboxes(seeded_db, tmp_path, broker_active):
    """Sanity companion: with `limit=None`, every inbox gets ingested.
    Without this baseline, a passing `test_..._decrements_across_inboxes`
    could be masking a regression that just never walks beta."""
    alpha_mirror = tmp_path / "alpha"
    alpha_mirror.mkdir()
    _build_pubinbox_repo(alpha_mirror / "0.git", [_rfc5322("a-nolim@example.com")])
    beta_mirror = tmp_path / "beta"
    beta_mirror.mkdir()
    _build_pubinbox_repo(beta_mirror / "0.git", [_rfc5322("b-nolim@example.com")])

    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        alpha.mirror_path = str(alpha_mirror)
        beta.mirror_path = str(beta_mirror)
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()

    out = ingest_all(inboxes={"alpha": alpha, "beta": beta}, limit=None, workers=1)
    assert "alpha" in out and "beta" in out


# --------------------------------------------------------------------------
# Multi-worker ingest (workers>1).
#
# Every other ingest test in this file pins `workers=1`. The
# CLAUDE.md rule "`parse_message` must stay importable at module
# level so it pickles cleanly to worker processes" has nothing
# enforcing it; a closure-capturing refactor would break production
# and pass CI. One end-to-end run with `workers=2` is enough to
# catch that.
# --------------------------------------------------------------------------
