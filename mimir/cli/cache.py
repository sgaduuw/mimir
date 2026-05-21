"""`warm-cache`: pre-recompute the slow dashboard queries so the web
tier serves them out of the DB-backed `cache` table.

Two phases: Phase A fans out across worker threads (per-inbox
helpers + sitemap caches + per-subsystem dashboard pre-warming);
Phase B runs serially after Phase A's barrier so the cross-inbox
aggregator (`most_active_subsystems_global`) reads the just-warmed
per-inbox cache rows instead of re-doing the underlying SQL.
"""
import os
import time
from datetime import datetime, timedelta, timezone

import click
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from mimir import cache as cache_mod
from mimir.dashboard import (
    archive_stats,
    author_recent,
    daily_volume,
    latest_pull_requests,
    latest_stable_releases,
    recent_articles,
    this_day_in_history,
)
from mimir.extensions import SessionLocal
from mimir.config import settings
from mimir.inboxes import bootstrap_inboxes
from mimir.models import Inbox, Subsystem
from mimir.subsystems_dashboard import (
    REVIEWS_PER_PAGE_LIMIT,
    active_reviewers_in_subsystem,
    active_threads_in_subsystem,
    articles_reviewed_by,
    daily_volume_in_subsystem,
    most_active_subsystems_global,
    most_active_subsystems_in_inbox,
    needs_attention_patches_in_subsystem,
    quiet_patches_in_subsystem,
    recent_articles_in_subsystem,
)
from mimir.threading import active_threads, threads_for_day
from mimir.web import (
    FEED_ENTRY_LIMIT,
    SUBSYSTEM_RECENT_PATCHES_LIMIT,
    inbox_sitemap_xml,
    meta_sitemap_xml,
    sitemap_index_xml,
)


# Refresh-window used by warm-cache. See the call site in
# `warm_cache_command` for the calibration rationale (cron period +
# jitter buffer). A key with less remaining TTL than this gets
# recomputed; one with more remaining TTL is left alone.
WARM_CACHE_REFRESH_WITHIN_SEC = 450

# Number of top-active subsystems whose per-subsystem dashboard
# caches are pre-warmed per inbox. The full per-subsystem dashboard
# fires four cache-backed helpers; on a 1500-subsystem corpus,
# warming every subsystem × every inbox would dominate the run.
# 20 covers the surfaces a reader most plausibly lands on (the
# front-page subsystem teaser and the per-inbox dashboard's
# "most active subsystems" widget both pull from the same ranked
# list). Long-tail subsystems still hit a single cold load per
# hour; subsequent visitors get the cached payload.
WARM_TOP_SUBSYSTEMS_PER_INBOX = 20


def _build_inbox_targets(
    inbox: Inbox,
    today: "datetime.date",
    yesterday: "datetime.date",
    sitemap_base: str = "",
) -> list[tuple[str, "object"]]:
    """Per-inbox warm-target list: one inbox in, list of
    `(label, fn)` tuples out. Each `fn(session)` computes one
    cached helper and (via `cache.get_or_compute`) writes the
    result back. Used by:

    - The legacy in-process `warm-cache` CLI, which iterates these
      across every configured inbox.
    - The broker's `handle_warm_inbox` handler (1.37.0), which
      runs them for one inbox per RPC so the broker's N warm-
      workers fan out across inboxes.

    Sitemap per-inbox row is included only when `sitemap_base` is
    non-empty (the cache key encodes the base URL, so warming with
    the wrong/empty base would poison the cache vs what the live
    route emits at request time).

    Splitting the per-inbox subsystem-dashboard target out into N
    per-subsystem tasks was considered (and rejected) in the 1.37.0
    design: the four helpers per subsystem are small individually
    but commit through the broker's writer-lock funnel anyway, so
    coalescing them into one task keeps the queue depth manageable
    without changing wall time.
    """
    targets: list[tuple[str, object]] = [
        (f"{inbox.name} active_threads (7d, 10)",
         lambda s, ib=inbox: active_threads(s, ib, days=7, limit=10)),
        (f"{inbox.name} threads_for_day (today)",
         lambda s, ib=inbox: threads_for_day(s, ib, today)),
        (f"{inbox.name} threads_for_day (yesterday)",
         lambda s, ib=inbox: threads_for_day(s, ib, yesterday)),
        (f"{inbox.name} daily_volume (30d)",
         lambda s, ib=inbox: daily_volume(s, ib, days=30)),
        (f"{inbox.name} archive_stats",
         lambda s, ib=inbox: archive_stats(s, ib)),
        (f"{inbox.name} latest_pull_requests",
         lambda s, ib=inbox: latest_pull_requests(s, ib, limit=5)),
        (f"{inbox.name} latest_stable_releases",
         lambda s, ib=inbox: latest_stable_releases(s, ib, limit=5)),
        (f"{inbox.name} this_day_in_history",
         lambda s, ib=inbox: this_day_in_history(s, ib, years_ago=5, limit=3)),
        # Atom feed source. Different cache key from the
        # dashboard "Recent messages" loader because the limit
        # is the cache key, feeds need 50, the dashboard's
        # initial paint uses 10.
        (f"{inbox.name} recent_articles ({FEED_ENTRY_LIMIT})",
         lambda s, ib=inbox: recent_articles(s, ib, limit=FEED_ENTRY_LIMIT)),
        # Per-inbox subsystem discoverability widget. One
        # warm target per inbox: the cache key is limit-less
        # since v1.19.3, so every caller (front-page top-12,
        # inbox dashboard top-10, cross-inbox aggregator)
        # slices from the same cached top-100 payload.
        (f"{inbox.name} most_active_subsystems_in_inbox (7d)",
         lambda s, ib=inbox: most_active_subsystems_in_inbox(s, ib, days=7)),
        # Per-subsystem dashboard helpers, top-N most active
        # subsystems only. Coarse-grained: one warm target per
        # inbox covering 4 helpers × top-N subsystems internally.
        # Long-tail subsystems pay one cold load per hour.
        (f"{inbox.name} subsystem dashboards (top {WARM_TOP_SUBSYSTEMS_PER_INBOX})",
         lambda s, ib=inbox: _warm_subsystem_dashboards(
             s, ib, WARM_TOP_SUBSYSTEMS_PER_INBOX,
         )),
    ]
    for label, substr in (inbox.tracked_authors or {}).items():
        # Dashboard tracker tile (limit=5) AND per-author atom
        # feed (limit=FEED_ENTRY_LIMIT) hit different cache
        # keys; warm both so the first feed poll per hour
        # gets a cache-hit just like the dashboard.
        targets.append((
            f"{inbox.name} tracker:{label}",
            lambda s, ib=inbox, sub=substr: author_recent(s, ib, sub, 5),
        ))
        targets.append((
            f"{inbox.name} tracker:{label} (feed)",
            lambda s, ib=inbox, sub=substr: author_recent(
                s, ib, sub, FEED_ENTRY_LIMIT,
            ),
        ))
    if sitemap_base:
        targets.append((
            f"sitemap:inbox:{inbox.name}",
            lambda s, ib=inbox, base=sitemap_base: inbox_sitemap_xml(s, ib, base),
        ))
    return targets


def _build_global_targets(
    sitemap_base: str = "",
) -> list[tuple[str, "object"]]:
    """Global (cross-inbox) warm targets: sitemap index, meta
    sitemap, and `most_active_subsystems_global`. The aggregator
    reads per-inbox cache rows, so it MUST run after every
    `_build_inbox_targets` cycle has completed (Phase B in the
    legacy CLI; a separate `warm_global` RPC in the broker shape).

    Sitemap targets are included only when `sitemap_base` is set:
    the helper caches a body keyed implicitly on the base URL, so
    warming with an empty base would poison the cache against
    what the route emits at request time."""
    targets: list[tuple[str, object]] = [(
        "most_active_subsystems_global (7d)",
        lambda s: most_active_subsystems_global(s, days=7),
    )]
    if sitemap_base:
        targets.insert(0, (
            "sitemap:index",
            lambda s, base=sitemap_base: sitemap_index_xml(s, base),
        ))
        targets.insert(1, (
            "sitemap:meta",
            lambda s, base=sitemap_base: meta_sitemap_xml(s, base),
        ))
    return targets


def _warm_subsystem_dashboards(session, inbox: Inbox, top_n: int) -> None:
    """Pre-warm the per-subsystem dashboard helpers AND the per-reviewer
    pages each one surfaces, for the top-N most active subsystems in
    `inbox`.

    Per subsystem we fire the four dashboard helpers
    (`recent_articles_in_subsystem`, `active_threads_in_subsystem`,
    `daily_volume_in_subsystem`, `active_reviewers_in_subsystem`); the
    helpers' arguments must match the `subsystem_dashboard` route call
    sites in `mimir/web/routes/dashboards.py`, or the cache keys
    diverge and the route still does cold work.

    On top of that, we capture the reviewer addresses surfaced by
    `active_reviewers_in_subsystem` across all top-N subsystems,
    dedup, and warm `articles_reviewed_by` for each. Prolific
    reviewers (Greg KH, david@kernel.org, etc.) appear under many
    subsystems so the dedup cuts the warm count roughly in half;
    typical bound is ~50-100 unique addresses per inbox. Pays the
    heavy `articles_reviewed_by` query on the warm-cache sidecar
    instead of the request path. External report 2026-05-16 (#195).
    """
    top = most_active_subsystems_in_inbox(session, inbox, days=7, limit=top_n)
    if not top:
        return
    # Bulk-load the Subsystems with their paths preloaded, one query
    # instead of N. The helpers below access `subsystem.paths` for the
    # F:/X: glob filters.
    sub_ids = [row.id for row in top]
    subs = session.execute(
        select(Subsystem)
        .options(selectinload(Subsystem.paths))
        .where(Subsystem.id.in_(sub_ids))
    ).scalars().all()
    sub_by_id = {s.id: s for s in subs}
    reviewer_addrs: set[str] = set()
    for row in top:
        sub = sub_by_id.get(row.id)
        if sub is None:
            continue
        # `refresh_window` is active in the calling context; each of
        # these falls through to a no-op when the cached row is far
        # from expiry.
        recent_articles_in_subsystem(
            session, inbox, sub, limit=SUBSYSTEM_RECENT_PATCHES_LIMIT,
        )
        active_threads_in_subsystem(session, inbox, sub, days=7, limit=10)
        daily_volume_in_subsystem(session, inbox, sub, days=30)
        # Triage queues (#209). Same arg shape as the route call
        # site so the cache key matches.
        needs_attention_patches_in_subsystem(session, inbox, sub, limit=10)
        quiet_patches_in_subsystem(session, inbox, sub, limit=10)
        # Collect addresses so we can warm the per-reviewer page each
        # one links to from this subsystem's "Active reviewers" list.
        for r in active_reviewers_in_subsystem(
            session, inbox, sub, days=30, limit=10,
        ):
            reviewer_addrs.add(r.address_normalized)
    # Warm articles_reviewed_by for each unique reviewer surfaced
    # above. Arguments must match the `reviewer_view` route call site
    # in `mimir/web/routes/search.py` (limit = REVIEWS_PER_PAGE_LIMIT)
    # or the cache key diverges and the warmed row never hits.
    for addr in reviewer_addrs:
        articles_reviewed_by(
            session, inbox, addr, limit=REVIEWS_PER_PAGE_LIMIT,
        )


@click.command("warm-cache")
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="-v: print per-key timings in addition to the summary line.",
)
@click.option(
    "--workers",
    "workers",
    type=int,
    default=None,
    help=(
        "Number of parallel workers (default: min(cpu_count, 8)). "
        "Pass 1 to disable parallelism when debugging."
    ),
)
def warm_cache_command(verbose: int, workers: int | None) -> None:
    """Recompute and cache the slow dashboard queries for every inbox.

    Designed to run from cron or a systemd timer. Refreshes the
    DB-backed `cache` table so the Flask server picks up pre-computed
    results on the next request, avoiding cold-start latency.

    Default output is one summary line per run; pass `-v` for the
    per-key timings.

    Example crontab:

        */5 * * * * cd ~/Projects/python/mimir && poetry run flask --app mimir warm-cache
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from contextvars import copy_context
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    inboxes = bootstrap_inboxes()
    sitemap_base = (settings.site_base_url or "").rstrip("/")
    worker_count = workers if workers is not None else min(os.cpu_count() or 1, 8)
    total_start = time.perf_counter()

    # Broker mode (Phase 2.2): fan out warm_inbox RPCs in parallel,
    # then fire warm_global once the per-inbox fan-out drains.
    # The broker's N warm-workers chew through the jobs concurrently.
    # The CLI-side ThreadPool is just a fan-out + collect pattern;
    # no work runs in this process beyond JSON encode/decode.
    if settings.broker_socket_path is not None:
        from mimir.broker.client import BrokerUnavailable, get_broker_client
        client = get_broker_client()
        total_keys = 0
        try:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = {
                    pool.submit(client.warm_inbox, name): name
                    for name in inboxes
                }
                for fut in as_completed(futures):
                    name = futures[fut]
                    result = fut.result()
                    warmed = result.get("warmed", [])
                    errors = result.get("errors", [])
                    elapsed_ms = result.get("elapsed_ms", 0)
                    total_keys += len(warmed)
                    if verbose:
                        click.echo(
                            f"{name}: {len(warmed)} keys "
                            f"warmed in {elapsed_ms} ms"
                            + (f" (errors: {len(errors)})" if errors else "")
                        )
            # Global Phase B after the per-inbox fan-out drains.
            global_result = client.warm_global()
            total_keys += len(global_result.get("warmed", []))
            if verbose:
                click.echo(
                    f"warm_global: {len(global_result.get('warmed', []))} "
                    f"keys warmed in "
                    f"{global_result.get('elapsed_ms', 0)} ms"
                )
        except BrokerUnavailable as exc:
            raise click.ClickException(
                f"broker warm-cache failed: {exc}"
            )
        total_ms = (time.perf_counter() - total_start) * 1000
        click.echo(
            f"warm-cache: {len(inboxes)} inbox{'' if len(inboxes) == 1 else 'es'}, "
            f"{total_keys} keys, {total_ms:.0f} ms total"
        )
        purged = cache_mod.purge_expired()
        if purged:
            click.echo(
                f"purged {purged} expired cache row"
                f"{'' if purged == 1 else 's'}"
            )
        return

    # Direct (non-broker) path: build target lists locally and run
    # them in this process with a ThreadPoolExecutor.
    phase_a: list[tuple[str, "object"]] = []
    for inbox in inboxes.values():
        phase_a.extend(_build_inbox_targets(inbox, today, yesterday, sitemap_base))
    # Phase B: global aggregators that read per-inbox cache rows.
    # `most_active_subsystems_global` MUST run after Phase A so it
    # picks up freshly-warmed per-inbox rows; with parallel Phase A
    # and no barrier, this target could race a worker still mid-
    # compute on its inbox's per-inbox key and end up doing the
    # work itself. Sitemap index/meta join Phase B in 1.37.0
    # because they're not per-inbox and have no good Phase A slot.
    phase_b: list[tuple[str, "object"]] = _build_global_targets(sitemap_base)
    total_keys = len(phase_a) + len(phase_b)

    def _run_target(label: str, fn) -> tuple[str, float]:
        """Open a fresh session per worker (SQLAlchemy sessions are not
        thread-safe) and run the target. Returns label + elapsed ms so
        the main thread can print -v lines and apply backpressure."""
        t0 = time.perf_counter()
        with SessionLocal() as session:
            fn(session)
        return label, (time.perf_counter() - t0) * 1000

    # TTL-aware refresh: a *cron-period* + jitter buffer. The cron
    # fires every 5 minutes; refreshing keys whose remaining TTL
    # is < ~7.5 min means 5-min-TTL keys (active_threads) refresh
    # every tick, 1-hour-TTL keys (daily_volume, listings) refresh
    # on the tick that lands inside the last 7.5 min of the hour,
    # and 24h-TTL keys (archive_stats) refresh on the tick nearest
    # their daily expiry. Previously every key was force-recomputed
    # on every tick regardless of TTL headroom.
    with cache_mod.refresh_window(WARM_CACHE_REFRESH_WITHIN_SEC):
        if worker_count <= 1:
            # Single-worker mode: the main-thread context already has
            # refresh_window set; no need to snapshot.
            for label, fn in phase_a:
                lbl, ms = _run_target(label, fn)
                if verbose:
                    click.echo(f"{lbl}: {ms:.0f} ms")
            for label, fn in phase_b:
                lbl, ms = _run_target(label, fn)
                if verbose:
                    click.echo(f"{lbl}: {ms:.0f} ms")
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                # Each submission snapshots the main-thread context
                # *inside* the with-block, so refresh_window is
                # captured. `Context.run` cannot be called concurrently
                # on the same Context object, so one fresh copy per
                # future is the safe pattern.
                futures = [
                    pool.submit(copy_context().run, _run_target, label, fn)
                    for label, fn in phase_a
                ]
                for fut in as_completed(futures):
                    lbl, ms = fut.result()
                    if verbose:
                        click.echo(f"{lbl}: {ms:.0f} ms")
            # Phase B serially on the main thread, its targets depend
            # on Phase A completion, and there's only one of them today.
            for label, fn in phase_b:
                lbl, ms = _run_target(label, fn)
                if verbose:
                    click.echo(f"{lbl}: {ms:.0f} ms")

    total_ms = (time.perf_counter() - total_start) * 1000
    click.echo(
        f"warm-cache: {len(inboxes)} inbox{'' if len(inboxes) == 1 else 'es'}, "
        f"{total_keys} keys, {total_ms:.0f} ms total"
    )

    # Drop expired rows so the cache table doesn't grow monotonically
    # as search keys, dated `threads_for_day:...:YYYY-MM-DD` keys, and
    # `monthly_volume:<inbox>:<year>` keys for past years accumulate.
    purged = cache_mod.purge_expired()
    if purged:
        click.echo(f"purged {purged} expired cache row{'' if purged == 1 else 's'}")
