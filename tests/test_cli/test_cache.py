"""Tests for mimir/cli/cache.py: `warm-cache` (worker plumbing,
verbose vs default output, per-key dashboard / reviewer /
atom-feed / sitemap targets), `analyze`, and `vacuum`
(post-vacuum size reporting)."""

from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from mimir.broker import _context
from mimir.broker.pools import ReadSessionPool
from mimir.broker.writes import WriterThread
from mimir.cli import (
    analyze_command,
    vacuum_command,
    warm_cache_command,
)
from mimir.inboxes import set_tracked_authors
from mimir.models import Article, ArticleList, Inbox


@pytest.fixture
def _active_broker_context():
    """Register an active pool + writer for the duration of each test.

    warm_cache_command dispatches warm_inbox / warm_global RPCs to the
    session broker; those handlers call _context.get_active_pool() and
    _context.get_active_writer(). Without a registration here, the warm
    handlers raise "No active broker; call set_active() first". The
    session-scoped _session_broker fixture provides the socket but does
    NOT register a pool+writer (that is per-test scope, owned here).

    Used as an explicit fixture argument on the tests that invoke
    warm_cache_command (not autouse, to avoid polluting tests that
    call cache.set() + cache.get() directly and need synchronous
    commit semantics). Saves and restores the pre-test context so any
    outer registration (from broker_running in other test modules) is
    not silently lost."""
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
        if saved_pool is not None and saved_writer is not None:
            _context.set_active(saved_pool, saved_writer)
        else:
            _context.clear_active()


def test_warm_cache_parallel_propagates_refresh_window(
    seeded_db, _active_broker_context
):
    """Worker threads must inherit the `refresh_window` contextvar
    via `copy_context()`. If they don't, a fresh-but-near-expiry
    cache row would *not* recompute under load, silently undoing
    Phase 2's TTL-aware refresh once Phase 3 fans out across workers.

    Pre-seed a near-expiry row for one warm target's key, then run
    warm-cache. The row's `expires_at` must move forward (recompute
    happened) instead of staying near-now (skipped)."""
    from sqlalchemy import delete as sql_delete
    from mimir.cache import _ns
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    # active_threads target for alpha; key matches dashboard helper.
    key = "active_threads:alpha:7:10"
    near_expiry = int(datetime.now(timezone.utc).timestamp()) + 30  # 30 s left
    with SessionLocal() as s:
        s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
        s.add(CacheEntry(key=_ns(key), value="[]", expires_at=near_expiry))
        s.commit()

    result = CliRunner().invoke(warm_cache_command, ["--workers", "4"])
    assert result.exit_code == 0

    with SessionLocal() as s:
        row = s.execute(
            select(CacheEntry).where(CacheEntry.key == _ns(key))
        ).scalar_one()
    # If the worker thread had no refresh_window, the still-live
    # 30s-remaining row would have short-circuited and `expires_at`
    # would still be near `near_expiry`. Successful refresh pushes
    # it out by the full TTL.
    assert row.expires_at > near_expiry + 60, (
        f"expected refresh to extend expires_at well past {near_expiry}, "
        f"got {row.expires_at}; refresh_window contextvar may not have "
        f"propagated to worker threads"
    )


def test_warm_cache_default_emits_only_summary(seeded_db, _active_broker_context):
    """Default verbosity collapses per-key timings into one summary
    line so the scheduler log doesn't scale with inbox count."""
    result = CliRunner().invoke(warm_cache_command, [])
    assert result.exit_code == 0
    lines = result.output.strip().splitlines()
    assert any(
        line.startswith("warm-cache:") and "ms total" in line for line in lines
    ), result.output
    # No per-key timing lines (those end with "<n> ms" without "total").
    per_key = [
        line for line in lines if line.endswith(" ms") and "ms total" not in line
    ]
    assert per_key == [], f"unexpected per-key lines at default verbosity: {per_key}"


def test_warm_cache_subsystem_dashboards_populate_cache(
    seeded_db, _active_broker_context
):
    """When an inbox has at least one most-active subsystem, the warm
    target writes cache rows for all four per-subsystem helpers. A
    silent regression in the helper composition (one helper dropped,
    wrong argument shape) wouldn't fail the timing-line assertion
    above but would leave a cold page; this asserts the actual rows."""
    from sqlalchemy import select as sa_select

    from mimir.extensions import SessionLocal
    from mimir.models import (
        ArticleFile,
        CacheEntry,
        Subsystem,
        SubsystemPath,
    )

    # Seed: one subsystem with one matching article, in 'alpha'.
    # Use a *relative* date so the article stays inside the
    # `most_active_subsystems_in_inbox` 7-day window regardless of
    # when the test runs. Hardcoded dates (the original shape) drift
    # past the window as wall-clock advances; we hit that on
    # 2026-05-20 when CI started failing the very test that had been
    # green the day before.
    recent = datetime.now(timezone.utc) - timedelta(days=2)
    with SessionLocal() as s:
        sub = Subsystem(name="BCACHEFS", status="Supported")
        s.add(sub)
        s.flush()
        s.add(SubsystemPath(subsystem_id=sub.id, glob="fs/bcachefs/", is_exclude=False))
        art = Article(
            message_id="warm-sub@x",
            subject="patch",
            author="A",
            date=recent,
            thread_parent=None,
            subject_normalized="patch",
        )
        s.add(art)
        s.flush()
        alpha = s.execute(sa_select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.add(ArticleFile(article_id=art.id, path="fs/bcachefs/super.c"))
        s.add(
            ArticleList(
                article_id=art.id,
                inbox_id=alpha.id,
                epoch="0.git",
                commit_sha="ab" * 20,
            )
        )
        s.commit()
        sub_id = sub.id

    # `--workers 1` forces the serial path. The default
    # `min(cpu_count, 8)` workers calls `cache.set()` concurrently
    # from worker threads against the same SQLite file; on CI runners
    # with many cores, the main thread's `cache.get()` below can race
    # the worker's commit-and-checkpoint visibility cycle even though
    # the `as_completed` join returned. The serial path is functionally
    # equivalent for this assertion (we're checking row presence, not
    # parallelism) and removes the flake.
    result = CliRunner().invoke(warm_cache_command, ["--workers", "1"])
    assert result.exit_code == 0

    # All four per-subsystem dashboard helpers must have cached a row
    # for this subsystem.
    expected_keys = [
        f"recent_articles_in_subsystem:alpha:{sub_id}:30",
        f"active_threads_in_subsystem:alpha:{sub_id}:7:10",
        f"daily_volume_in_subsystem:alpha:{sub_id}:30",
        f"active_reviewers_in_subsystem:alpha:{sub_id}:30:10",
    ]
    from mimir.cache import _ns

    with SessionLocal() as s:
        present = {
            row.key
            for row in s.execute(
                sa_select(CacheEntry).where(
                    CacheEntry.key.in_([_ns(k) for k in expected_keys])
                )
            )
            .scalars()
            .all()
        }
    for k in expected_keys:
        assert _ns(k) in present, (
            f"warm-cache did not populate {k}; per-subsystem dashboard "
            f"will be cold on first visit"
        )


def test_warm_cache_warms_reviewer_pages_from_per_subsystem_dashboards(
    seeded_db,
    _active_broker_context,
):
    """Per-reviewer page (`/<inbox>/reviewer/<addr>`) is reached via
    each per-subsystem dashboard's "Active reviewers" list. warm-cache
    must collect those addresses across the top-N subsystems it
    warms, dedup, and pre-warm `articles_reviewed_by` for each so
    the page is instant on first click. #195. Arguments must match
    the route's `articles_reviewed_by` call site
    (limit=REVIEWS_PER_PAGE_LIMIT = 100) or the cache key diverges."""
    from sqlalchemy import select as sa_select

    from mimir.extensions import SessionLocal
    from mimir.models import (
        ArticleFile,
        ArticleTrailer,
        CacheEntry,
        Subsystem,
        SubsystemPath,
    )

    # Seed: one subsystem, one recent in-subsystem article with one
    # review trailer. The address is what active_reviewers_in_subsystem
    # will surface and what we expect warm-cache to look up via
    # articles_reviewed_by. Relative date (see companion test above)
    # so the article stays inside the 7-day "most active" window
    # regardless of when the test runs.
    recent = datetime.now(timezone.utc) - timedelta(days=2)
    with SessionLocal() as s:
        sub = Subsystem(name="BCACHEFS", status="Supported")
        s.add(sub)
        s.flush()
        s.add(
            SubsystemPath(
                subsystem_id=sub.id,
                glob="fs/bcachefs/",
                is_exclude=False,
            )
        )
        alpha = s.execute(sa_select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        art = Article(
            message_id="reviewer-warm@x",
            subject="patch with reviewer",
            author="A",
            date=recent,
            thread_parent=None,
            subject_normalized="patch with reviewer",
        )
        s.add(art)
        s.flush()
        s.add(ArticleFile(article_id=art.id, path="fs/bcachefs/super.c"))
        s.add(
            ArticleList(
                article_id=art.id,
                inbox_id=alpha.id,
                epoch="0.git",
                commit_sha="cd" * 20,
            )
        )
        s.add(
            ArticleTrailer(
                article_id=art.id,
                role="Reviewed-by",
                name="David Reviewer",
                address="david@kernel.org",
                address_normalized="david@kernel.org",
            )
        )
        s.commit()

    # `--workers 1` for the same reason the sibling test uses it (avoid
    # the parallel-commit-visibility race against the assertion below).
    result = CliRunner().invoke(warm_cache_command, ["--workers", "1"])
    assert result.exit_code == 0

    from mimir.cache import _ns

    expected_key = "articles_reviewed_by:alpha:david@kernel.org:100"
    with SessionLocal() as s:
        present = s.execute(
            sa_select(CacheEntry).where(CacheEntry.key == _ns(expected_key))
        ).scalar_one_or_none()
    assert present is not None, (
        f"warm-cache did not populate {expected_key}; the reviewer page "
        f"will be cold on first visit"
    )


def test_warm_cache_includes_atom_feed_sources(seeded_db, _active_broker_context):
    """The atom routes use `recent_articles(limit=FEED_ENTRY_LIMIT)`
    and `author_recent(..., limit=FEED_ENTRY_LIMIT)`, a different
    cache key from the dashboard's `limit=5/10` calls. Warm both so
    the first feed poll per hour returns a cache-hit too."""
    from mimir import cache

    set_tracked_authors("alpha", {"Examples": "example.com"})
    result = CliRunner().invoke(warm_cache_command, [])
    assert result.exit_code == 0
    # Feed-flavour and tracker rows must actually land in the cache
    # (the legacy per-target verbose echo went into broker logs).
    assert cache.get("recent_articles:alpha:50") is not None
    assert cache.get("recent_articles:beta:50") is not None
    # author_recent key uses the email substring directly, not the label.
    assert cache.get("author_recent:alpha:example.com:5") is not None
    assert cache.get("author_recent:alpha:example.com:50") is not None


def test_warm_cache_skips_sitemap_when_site_base_url_unset(
    seeded_db, monkeypatch, _active_broker_context
):
    """Without SITE_BASE_URL, sitemap renders rely on `request.url_root`
    which isn't available from the CLI. Warm-cache skips them rather
    than poison the cache with relative-looking URLs."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "site_base_url", "")
    result = CliRunner().invoke(warm_cache_command, ["-v"])
    assert result.exit_code == 0
    assert "sitemap:index" not in result.output
    assert "sitemap:meta" not in result.output
    assert "sitemap:inbox:" not in result.output


def test_warm_cache_includes_sitemap_when_site_base_url_set(
    seeded_db, monkeypatch, _active_broker_context
):
    """With SITE_BASE_URL set, warm-cache pre-renders the three
    sitemap surfaces, index, meta, and per-inbox, so the first
    crawler hit per hour gets a cache-hit."""
    from sqlalchemy import delete
    from mimir import cache
    from mimir.config import settings
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    monkeypatch.setattr(settings, "site_base_url", "https://example.test")
    # Clean slate so we can assert the entries exist post-run.
    with SessionLocal() as s:
        s.execute(delete(CacheEntry))
        s.commit()

    # `--workers 1` forces the serial path. See the matching comment
    # on `test_warm_cache_subsystem_dashboards_populate_cache` for the
    # CI flake this avoids: under default parallelism, worker-thread
    # cache writes can lag the main-thread reads below even after
    # `as_completed` reports done.
    result = CliRunner().invoke(warm_cache_command, ["--workers", "1"])
    assert result.exit_code == 0

    # Cache rows actually landed and decode to well-formed XML with
    # the right schema-namespaced root element. A "<?xml" prefix
    # check alone would pass on a malformed document.
    import xml.etree.ElementTree as ET

    ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    expected_root = {
        "sitemap:index": "{%s}sitemapindex" % ns,
        "sitemap:meta": "{%s}urlset" % ns,
        "sitemap:inbox:alpha": "{%s}urlset" % ns,
        "sitemap:inbox:beta": "{%s}urlset" % ns,
    }
    for key, expected_tag in expected_root.items():
        payload = cache.get(key)
        assert payload is not None, f"missing cache row for {key}"
        # The cache now stores SitemapPayload (body + last_modified);
        # the body field carries the XML the route serves.
        assert "example.test" in payload.body, (
            f"cache row for {key} doesn't carry the SITE_BASE_URL prefix; "
            f"warm-cache may have run with the wrong base"
        )
        root = ET.fromstring(payload.body)
        assert root.tag == expected_tag, (
            f"cache row for {key} has wrong root element: {root.tag!r} "
            f"(expected {expected_tag!r})"
        )


def test_warm_cache_sitemap_helpers_force_recompute(seeded_db):
    """Passing force=True to the sitemap helpers must overwrite the
    cached value, even when a live (unexpired) row exists. Without
    this, warm-cache after a fresh ingest wouldn't see the new
    article URLs until the 1h TTL elapsed."""
    from sqlalchemy import select
    from mimir import cache
    from mimir.extensions import SessionLocal
    from mimir.seo import inbox_sitemap_xml

    cache.set("sitemap:inbox:alpha", "STALE", ttl=3600)
    assert cache.get("sitemap:inbox:alpha") == "STALE"
    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        payload = inbox_sitemap_xml(s, alpha, "https://example.test", force=True)
    assert "<?xml" in payload.body
    # The cache now stores the full SitemapPayload (body + last_modified);
    # `force=True` recomputed it, so the cached value equals the returned
    # payload exactly.
    assert cache.get("sitemap:inbox:alpha") == payload


def test_analyze_command_runs(seeded_db):
    """`ANALYZE` is a no-op-shaped command on tiny DBs, but the CLI's
    contract is: runs, returns 0, prints the timing line."""
    result = CliRunner().invoke(analyze_command, [])
    assert result.exit_code == 0, result.output
    assert "ANALYZE complete" in result.output


def test_analyze_command_full_flag_runs(seeded_db):
    """`mimir analyze --full` (1.36.4) runs ANALYZE with
    `analysis_limit=0` overriding the per-connection default cap
    (`settings.analyze_limit`). On a tiny test DB the behaviour is
    indistinguishable from the default flow; the contract being
    pinned is just that the flag is wired and the command's
    distinct output line lands so the scheduler loop's log makes
    `analyze --full` legible vs the default daily `analyze`."""
    result = CliRunner().invoke(analyze_command, ["--full"])
    assert result.exit_code == 0, result.output
    assert "full ANALYZE complete" in result.output


def test_vacuum_command_runs_and_reports_sizes(seeded_db):
    """`vacuum` rebuilds the DB and checkpoints the WAL. On a tiny
    test DB the reclamation is negligible, but the before/after/
    reclaimed lines must all emit and the DB must still be openable
    afterwards."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal

    result = CliRunner().invoke(vacuum_command, [])
    assert result.exit_code == 0, result.output
    assert "before:" in result.output
    assert "after:" in result.output
    assert "reclaimed" in result.output

    # The DB is still usable after the engine.dispose() inside vacuum.
    with SessionLocal() as s:
        names = s.execute(select(Inbox.name)).scalars().all()
    assert "alpha" in names


# `admin failures list` / `replay` -- CLI wrappers around the service
# layer (which has its own coverage in test_ingest.py).


# Fast/slow tier split (Task 3 of 2026-06-01-warm-cache-fast-slow-
# tier-split). The combined `_build_inbox_targets` /
# `_build_global_targets` are partitioned into fast + slow halves
# so the scheduler can fire fast targets every minute and slow
# targets every hour; the combined builders survive as wrappers
# so `mimir warm-cache --tier all` (operator one-off) and any
# not-yet-migrated callers see today's full target list.


def test_build_fast_inbox_targets_includes_sitemap_when_base_set(seeded_db):
    """Fast tier per-inbox builder returns sitemap + the four
    cheap helpers (archive_stats, latest_pull_requests,
    latest_stable_releases, recent_articles) when SITE_BASE_URL is
    set."""
    from mimir.cli.cache import _build_fast_inbox_targets
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
    targets = _build_fast_inbox_targets(alpha, sitemap_base="https://example.test")
    labels = [label for label, _fn in targets]
    assert any(label.startswith("sitemap:inbox:alpha") for label in labels)
    assert any("archive_stats" in label for label in labels)
    assert any("latest_pull_requests" in label for label in labels)
    assert any("latest_stable_releases" in label for label in labels)
    assert any("recent_articles" in label for label in labels)


def test_build_fast_inbox_targets_omits_sitemap_without_base(seeded_db):
    """No SITE_BASE_URL → no sitemap target, but the four cheap
    helpers still come back."""
    from mimir.cli.cache import _build_fast_inbox_targets
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
    targets = _build_fast_inbox_targets(alpha, sitemap_base="")
    labels = [label for label, _fn in targets]
    assert not any("sitemap" in label for label in labels)
    assert any("archive_stats" in label for label in labels)


def test_build_slow_inbox_targets_includes_subsystem_dashboards(seeded_db):
    """Slow tier per-inbox: subsystem dashboards + per-tracker +
    daily_volume + threads_for_day + active_threads +
    most_active_subsystems_in_inbox + this_day_in_history."""
    import datetime

    from mimir.cli.cache import _build_slow_inbox_targets
    from mimir.extensions import SessionLocal

    today = datetime.date(2024, 3, 1)
    yesterday = today - datetime.timedelta(days=1)
    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
    targets = _build_slow_inbox_targets(alpha, today, yesterday)
    labels = [label for label, _fn in targets]
    assert any("subsystem dashboards" in label for label in labels)
    assert any("daily_volume" in label for label in labels)
    assert any("threads_for_day (today)" in label for label in labels)
    assert any("threads_for_day (yesterday)" in label for label in labels)
    assert any("active_threads" in label for label in labels)
    assert any("most_active_subsystems_in_inbox" in label for label in labels)
    assert any("this_day_in_history" in label for label in labels)


def test_build_inbox_targets_concatenates_fast_then_slow(seeded_db):
    """Legacy combined builder = fast + slow union by label
    (set-equality). The fast and slow halves partition cleanly
    (no overlap, no drops); total count == fast + slow."""
    import datetime

    from mimir.cli.cache import (
        _build_fast_inbox_targets,
        _build_inbox_targets,
        _build_slow_inbox_targets,
    )
    from mimir.extensions import SessionLocal

    today = datetime.date(2024, 3, 1)
    yesterday = today - datetime.timedelta(days=1)
    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
    full = _build_inbox_targets(
        alpha, today, yesterday, sitemap_base="https://example.test"
    )
    fast = _build_fast_inbox_targets(alpha, sitemap_base="https://example.test")
    slow = _build_slow_inbox_targets(alpha, today, yesterday)
    full_labels = {label for label, _ in full}
    union_labels = {label for label, _ in fast} | {label for label, _ in slow}
    assert full_labels == union_labels
    # No duplicates (fast and slow partition cleanly).
    assert len(full) == len(fast) + len(slow)


def test_build_fast_global_targets_includes_sitemap_keys(seeded_db):
    """Global fast tier: sitemap:index + sitemap:meta only."""
    from mimir.cli.cache import _build_fast_global_targets

    targets = _build_fast_global_targets(sitemap_base="https://example.test")
    labels = [label for label, _fn in targets]
    assert "sitemap:index" in labels
    assert "sitemap:meta" in labels
    assert not any("most_active_subsystems" in label for label in labels)


def test_build_fast_global_targets_omits_sitemap_without_base(seeded_db):
    """No SITE_BASE_URL → empty list (sitemap targets are gated; no
    other fast-tier globals exist)."""
    from mimir.cli.cache import _build_fast_global_targets

    targets = _build_fast_global_targets(sitemap_base="")
    assert targets == []


def test_build_slow_global_targets_includes_aggregator(seeded_db):
    """Global slow tier: just most_active_subsystems_global (7d)."""
    from mimir.cli.cache import _build_slow_global_targets

    targets = _build_slow_global_targets()
    labels = [label for label, _fn in targets]
    assert any("most_active_subsystems_global" in label for label in labels)
    assert not any("sitemap" in label for label in labels)


# `--tier {fast,slow,all}` CLI dispatch (Task 4 of 2026-06-01-warm-
# cache-fast-slow-tier-split). The flag drives `targets=` filtering on
# the warm_inbox / warm_global broker RPCs: targets=None means "broker
# picks the full set" (today's shape, back-compat); an explicit list
# narrows the dispatch to just the named labels.
#
# These tests intercept the broker client by monkeypatching
# `mimir.broker.client.get_broker_client` (the in-function import in
# `warm_cache_command` reads through the module attribute, so the
# patch lands). The FakeClient stubs `warm_inbox` / `warm_global` to
# record the targets passed by the CLI, and stubs `cache_purge_expired`
# because `cache.purge_expired()` (called at the end of
# `warm_cache_command`) also routes through `get_broker_client()`.


def test_warm_cache_command_tier_fast_dispatches_fast_targets_only(
    seeded_db, monkeypatch
):
    """`warm-cache --tier fast` ends up dispatching each warm_inbox
    RPC with only fast-tier target labels (sitemap + archive_stats
    + latest_pull_requests + latest_stable_releases + recent_articles).
    Slow-tier labels (subsystem dashboards, threads_for_day, etc.)
    don't appear."""
    from mimir.broker import client as _client_module
    from mimir.cli.cache import warm_cache_command
    from mimir.config import settings

    captured: list[tuple[str, list[str] | None]] = []
    global_captured: list[list[str] | None] = []

    class FakeClient:
        def warm_inbox(self, inbox_name, *, targets=None, **kwargs):
            captured.append((inbox_name, targets))
            return {"warmed": targets or [], "errors": [], "elapsed_ms": 0}

        def warm_global(self, *, targets=None, **kwargs):
            global_captured.append(targets)
            return {"warmed": targets or [], "errors": [], "elapsed_ms": 0}

        def cache_purge_expired(self, **kwargs):
            return 0

    monkeypatch.setattr(_client_module, "get_broker_client", lambda: FakeClient())
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")

    result = CliRunner().invoke(warm_cache_command, ["--tier", "fast"])
    assert result.exit_code == 0, result.output
    assert captured, "no warm_inbox dispatches captured"
    for inbox_name, targets in captured:
        assert targets is not None, (
            f"tier=fast must pass explicit targets, got None for {inbox_name}"
        )
        # Each fast-tier label must be one of the documented fast set.
        for label in targets:
            assert any(
                fragment in label
                for fragment in (
                    "sitemap:inbox:",
                    "archive_stats",
                    "latest_pull_requests",
                    "latest_stable_releases",
                    "recent_articles",
                )
            ), f"non-fast label leaked into tier=fast dispatch: {label!r}"
        # No slow-tier label appears.
        assert not any(
            fragment in label
            for fragment in (
                "subsystem dashboards",
                "threads_for_day",
                "daily_volume",
                "active_threads",
                "this_day_in_history",
                "most_active_subsystems_in_inbox",
                "tracker:",
            )
            for label in targets
        )
    # Global fast tier hits sitemap:index + sitemap:meta.
    assert global_captured, "no warm_global dispatch captured"
    assert all(
        "sitemap" in label
        for targets in global_captured
        if targets
        for label in targets
    )


def test_warm_cache_command_tier_slow_dispatches_slow_targets_only(
    seeded_db, monkeypatch
):
    """Mirror: `warm-cache --tier slow` dispatches only slow-tier
    target labels."""
    from mimir.broker import client as _client_module
    from mimir.cli.cache import warm_cache_command
    from mimir.config import settings

    captured: list[tuple[str, list[str] | None]] = []

    class FakeClient:
        def warm_inbox(self, inbox_name, *, targets=None, **kwargs):
            captured.append((inbox_name, targets))
            return {"warmed": targets or [], "errors": [], "elapsed_ms": 0}

        def warm_global(self, *, targets=None, **kwargs):
            return {"warmed": targets or [], "errors": [], "elapsed_ms": 0}

        def cache_purge_expired(self, **kwargs):
            return 0

    monkeypatch.setattr(_client_module, "get_broker_client", lambda: FakeClient())
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")

    result = CliRunner().invoke(warm_cache_command, ["--tier", "slow"])
    assert result.exit_code == 0, result.output
    assert captured
    for inbox_name, targets in captured:
        assert targets is not None
        # No fast-tier label appears.
        assert not any(
            fragment in label
            for fragment in (
                "sitemap:inbox:",
                "archive_stats",
                "latest_pull_requests",
                "latest_stable_releases",
                "recent_articles",
            )
            for label in targets
        ), f"fast label leaked into tier=slow dispatch for {inbox_name}: {targets}"


def test_warm_cache_command_default_tier_all_passes_no_targets(seeded_db, monkeypatch):
    """Default (no --tier) preserves today's behaviour: targets=None
    on every dispatch, so the broker handler uses its full target
    list."""
    from mimir.broker import client as _client_module
    from mimir.cli.cache import warm_cache_command
    from mimir.config import settings

    captured: list[tuple[str, list[str] | None]] = []
    global_captured: list[list[str] | None] = []

    class FakeClient:
        def warm_inbox(self, inbox_name, *, targets=None, **kwargs):
            captured.append((inbox_name, targets))
            return {"warmed": [], "errors": [], "elapsed_ms": 0}

        def warm_global(self, *, targets=None, **kwargs):
            global_captured.append(targets)
            return {"warmed": [], "errors": [], "elapsed_ms": 0}

        def cache_purge_expired(self, **kwargs):
            return 0

    monkeypatch.setattr(_client_module, "get_broker_client", lambda: FakeClient())
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")

    result = CliRunner().invoke(warm_cache_command, [])
    assert result.exit_code == 0, result.output
    assert captured, "no warm_inbox dispatches captured"
    for inbox_name, targets in captured:
        assert targets is None, (
            f"tier=all should pass targets=None for {inbox_name}; got {targets}"
        )
    for targets in global_captured:
        assert targets is None
