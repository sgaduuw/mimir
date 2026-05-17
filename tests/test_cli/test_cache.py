"""Tests for mimir/cli/cache.py: `warm-cache` (worker plumbing,
verbose vs default output, per-key dashboard / reviewer /
atom-feed / sitemap targets), `analyze`, and `vacuum`
(post-vacuum size reporting)."""


from datetime import datetime, timezone

from click.testing import CliRunner
from sqlalchemy import select

from mimir.cli import (
    analyze_command,
    vacuum_command,
    warm_cache_command,
)
from mimir.inboxes import set_tracked_authors
from mimir.models import Article, ArticleList, Inbox


def test_warm_cache_workers_one_serial_path(seeded_db):
    """`--workers 1` forces the single-threaded path that bypasses the
    thread pool. Behavior must match the parallel path: every key
    still gets warmed, summary line still emits."""
    result = CliRunner().invoke(warm_cache_command, ["--workers", "1", "-v"])
    assert result.exit_code == 0
    per_key_lines = [
        line for line in result.output.splitlines()
        if line.endswith(" ms") and "ms total" not in line
    ]
    assert len(per_key_lines) >= 5, result.output
    assert "warm-cache:" in result.output and "ms total" in result.output


def test_warm_cache_parallel_propagates_refresh_window(seeded_db):
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


def test_warm_cache_default_emits_only_summary(seeded_db):
    """Default verbosity collapses per-key timings into one summary
    line so the scheduler log doesn't scale with inbox count."""
    result = CliRunner().invoke(warm_cache_command, [])
    assert result.exit_code == 0
    lines = result.output.strip().splitlines()
    assert any(line.startswith("warm-cache:") and "ms total" in line for line in lines), result.output
    # No per-key timing lines (those end with "<n> ms" without "total").
    per_key = [
        line for line in lines
        if line.endswith(" ms") and "ms total" not in line
    ]
    assert per_key == [], f"unexpected per-key lines at default verbosity: {per_key}"


def test_warm_cache_verbose_keeps_per_key_timings(seeded_db):
    """-v restores the per-key timings on top of the summary line.

    Structural: each seeded inbox must get *multiple* per-key timing
    lines (one per cached helper -- archive_stats, active_threads,
    daily_volume, etc.). Pinning the literal label names ties the
    test to incidental cache-key strings; asserting "each inbox got
    several timings" catches the only regression that matters --
    warm-cache stopped iterating an inbox -- without flagging on a
    benign rename."""
    result = CliRunner().invoke(warm_cache_command, ["-v"])
    assert result.exit_code == 0
    per_key_lines = [
        line for line in result.output.splitlines()
        if line.endswith(" ms") and "ms total" not in line
    ]
    alpha_lines = [line for line in per_key_lines if line.startswith("alpha ")]
    beta_lines = [line for line in per_key_lines if line.startswith("beta ")]
    # Lower bound, not exact: more helpers may join the warm-cache
    # rotation; fewer means an inbox stopped being iterated.
    assert len(alpha_lines) >= 5, (
        f"alpha got only {len(alpha_lines)} per-key lines: {alpha_lines}"
    )
    assert len(beta_lines) >= 5, (
        f"beta got only {len(beta_lines)} per-key lines: {beta_lines}"
    )
    # Summary line is still there.
    assert "warm-cache:" in result.output and "ms total" in result.output


def test_warm_cache_includes_subsystem_dashboard_targets(seeded_db):
    """Per-subsystem dashboard helpers (recent_articles_in_subsystem +
    three siblings) drive the slowest cold-load page. warm-cache must
    iterate the top-N subsystems per inbox so the second visitor lands
    on a warmed cache."""
    result = CliRunner().invoke(warm_cache_command, ["-v"])
    assert result.exit_code == 0
    assert "alpha subsystem dashboards (top 20)" in result.output
    assert "beta subsystem dashboards (top 20)" in result.output


def test_warm_cache_subsystem_dashboards_populate_cache(seeded_db):
    """When an inbox has at least one most-active subsystem, the warm
    target writes cache rows for all four per-subsystem helpers. A
    silent regression in the helper composition (one helper dropped,
    wrong argument shape) wouldn't fail the timing-line assertion
    above but would leave a cold page; this asserts the actual rows."""
    from sqlalchemy import select as sa_select

    from mimir.extensions import SessionLocal
    from mimir.models import (
        ArticleFile, CacheEntry, Subsystem,
        SubsystemPath,
    )

    # Seed: one subsystem with one matching article, in 'alpha'.
    with SessionLocal() as s:
        sub = Subsystem(name="BCACHEFS", status="Supported")
        s.add(sub)
        s.flush()
        s.add(SubsystemPath(subsystem_id=sub.id, glob="fs/bcachefs/", is_exclude=False))
        art = Article(
            message_id="warm-sub@x", subject="patch", author="A",
            date=datetime(2026, 5, 14, tzinfo=timezone.utc),
            thread_parent=None, subject_normalized="patch",
        )
        s.add(art)
        s.flush()
        alpha = s.execute(sa_select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.add(ArticleFile(article_id=art.id, path="fs/bcachefs/super.c"))
        s.add(ArticleList(
            article_id=art.id, inbox_id=alpha.id, epoch="0.git",
            commit_sha="ab" * 20,
        ))
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
            row.key for row in s.execute(
                sa_select(CacheEntry).where(
                    CacheEntry.key.in_([_ns(k) for k in expected_keys])
                )
            ).scalars().all()
        }
    for k in expected_keys:
        assert _ns(k) in present, (
            f"warm-cache did not populate {k}; per-subsystem dashboard "
            f"will be cold on first visit"
        )


def test_warm_cache_warms_reviewer_pages_from_per_subsystem_dashboards(
    seeded_db,
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
        ArticleFile, ArticleTrailer, CacheEntry,
        Subsystem, SubsystemPath,
    )

    # Seed: one subsystem, one recent in-subsystem article with one
    # review trailer. The address is what active_reviewers_in_subsystem
    # will surface and what we expect warm-cache to look up via
    # articles_reviewed_by.
    with SessionLocal() as s:
        sub = Subsystem(name="BCACHEFS", status="Supported")
        s.add(sub)
        s.flush()
        s.add(SubsystemPath(
            subsystem_id=sub.id, glob="fs/bcachefs/", is_exclude=False,
        ))
        alpha = s.execute(
            sa_select(Inbox).where(Inbox.name == "alpha")
        ).scalar_one()
        art = Article(
            message_id="reviewer-warm@x",
            subject="patch with reviewer",
            author="A",
            date=datetime(2026, 5, 14, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized="patch with reviewer",
        )
        s.add(art)
        s.flush()
        s.add(ArticleFile(article_id=art.id, path="fs/bcachefs/super.c"))
        s.add(ArticleList(
            article_id=art.id, inbox_id=alpha.id, epoch="0.git",
            commit_sha="cd" * 20,
        ))
        s.add(ArticleTrailer(
            article_id=art.id, role="Reviewed-by",
            name="David Reviewer", address="david@kernel.org",
            address_normalized="david@kernel.org",
        ))
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


def test_warm_cache_includes_atom_feed_sources(seeded_db):
    """The atom routes use `recent_articles(limit=FEED_ENTRY_LIMIT)`
    and `author_recent(..., limit=FEED_ENTRY_LIMIT)`, a different
    cache key from the dashboard's `limit=5/10` calls. Warm both so
    the first feed poll per hour returns a cache-hit too."""
    set_tracked_authors("alpha", {"Examples": "example.com"})
    result = CliRunner().invoke(warm_cache_command, ["-v"])
    assert result.exit_code == 0
    # Recent feed flavour for each seeded inbox.
    assert "alpha recent_articles (50)" in result.output
    assert "beta recent_articles (50)" in result.output
    # Tracker tile + feed flavour distinct lines.
    assert "alpha tracker:Examples" in result.output
    assert "alpha tracker:Examples (feed)" in result.output


def test_warm_cache_skips_sitemap_when_site_base_url_unset(
    seeded_db, monkeypatch
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
    seeded_db, monkeypatch
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
    result = CliRunner().invoke(warm_cache_command, ["-v", "--workers", "1"])
    assert result.exit_code == 0
    assert "sitemap:index" in result.output
    assert "sitemap:meta" in result.output
    assert "sitemap:inbox:alpha" in result.output
    assert "sitemap:inbox:beta" in result.output

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
        body = cache.get(key)
        assert body is not None, f"missing cache row for {key}"
        assert "example.test" in body, (
            f"cache row for {key} doesn't carry the SITE_BASE_URL prefix; "
            f"warm-cache may have run with the wrong base"
        )
        root = ET.fromstring(body)
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
        body = inbox_sitemap_xml(s, alpha, "https://example.test", force=True)
    assert "<?xml" in body
    assert cache.get("sitemap:inbox:alpha") == body


def test_analyze_command_runs(seeded_db):
    """`ANALYZE` is a no-op-shaped command on tiny DBs, but the CLI's
    contract is: runs, returns 0, prints the timing line."""
    result = CliRunner().invoke(analyze_command, [])
    assert result.exit_code == 0, result.output
    assert "ANALYZE complete" in result.output


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
