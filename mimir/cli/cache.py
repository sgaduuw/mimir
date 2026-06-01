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
from datetime import date, datetime, timedelta

import click
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from mimir import cache as cache_mod
from mimir.config import settings
from mimir.dashboard import (
    archive_stats,
    author_recent,
    daily_volume,
    latest_pull_requests,
    latest_stable_releases,
    recent_articles,
    this_day_in_history,
)
from mimir.inboxes import list_inboxes
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


def _build_fast_inbox_targets(
    inbox: Inbox,
    sitemap_base: str = "",
) -> list[tuple[str, "object"]]:
    """Fast tier per-inbox warm targets: the cheap, freshness-
    sensitive helpers that the front page and crawlers read
    constantly. Sub-100 ms each; the whole list is ~300-500 ms
    per inbox. Designed to fire on the per-minute scheduler tick
    (`mimir warm-cache --tier fast`), independent of the slow
    tier's per-hour cadence.

    Includes: archive_stats, latest_pull_requests,
    latest_stable_releases, recent_articles (FEED_ENTRY_LIMIT),
    and the per-inbox sitemap (only when sitemap_base is non-empty;
    the helper caches a body keyed implicitly on the base URL, so
    warming with an empty base would poison the cache vs what the
    live route emits)."""
    targets: list[tuple[str, object]] = [
        (f"{inbox.name} archive_stats", lambda s, ib=inbox: archive_stats(s, ib)),
        (
            f"{inbox.name} latest_pull_requests",
            lambda s, ib=inbox: latest_pull_requests(s, ib, limit=5),
        ),
        (
            f"{inbox.name} latest_stable_releases",
            lambda s, ib=inbox: latest_stable_releases(s, ib, limit=5),
        ),
        # Atom feed source. Different cache key from the
        # dashboard "Recent messages" loader because the limit
        # is the cache key, feeds need 50, the dashboard's
        # initial paint uses 10.
        (
            f"{inbox.name} recent_articles ({FEED_ENTRY_LIMIT})",
            lambda s, ib=inbox: recent_articles(s, ib, limit=FEED_ENTRY_LIMIT),
        ),
    ]
    if sitemap_base:
        targets.append(
            (
                f"sitemap:inbox:{inbox.name}",
                lambda s, ib=inbox, base=sitemap_base: inbox_sitemap_xml(s, ib, base),
            )
        )
    return targets


def _build_slow_inbox_targets(
    inbox: Inbox,
    today: "datetime.date",
    yesterday: "datetime.date",
) -> list[tuple[str, "object"]]:
    """Slow tier per-inbox warm targets: the heavy queries
    dominated by subsystem-dashboard cost (50-100 s per inbox on
    broad-F: subsystems like linux-arm-kernel). Designed to fire on
    the per-hour scheduler tick (`mimir warm-cache --tier slow`).

    Includes: active_threads, threads_for_day (today + yesterday),
    daily_volume, this_day_in_history, most_active_subsystems_in_inbox,
    the per-subsystem dashboards fan-out, and the per-tracker pairs
    (dashboard tile + per-author atom feed) from inbox.tracked_authors.

    `today` / `yesterday` are passed explicitly because the cache key
    for `threads_for_day` includes the date; the scheduler computes
    them once per cycle to keep both inbox-level calls aligned.

    Splitting the per-inbox subsystem-dashboard target out into N
    per-subsystem tasks was considered (and rejected) in the 1.37.0
    design: the four helpers per subsystem are small individually
    but commit through the broker's writer-lock funnel anyway, so
    coalescing them into one task keeps the queue depth manageable
    without changing wall time.
    """
    targets: list[tuple[str, object]] = [
        (
            f"{inbox.name} active_threads (7d, 10)",
            lambda s, ib=inbox: active_threads(s, ib, days=7, limit=10),
        ),
        (
            f"{inbox.name} threads_for_day (today)",
            lambda s, ib=inbox: threads_for_day(s, ib, today),
        ),
        (
            f"{inbox.name} threads_for_day (yesterday)",
            lambda s, ib=inbox: threads_for_day(s, ib, yesterday),
        ),
        (
            f"{inbox.name} daily_volume (30d)",
            lambda s, ib=inbox: daily_volume(s, ib, days=30),
        ),
        (
            f"{inbox.name} this_day_in_history",
            lambda s, ib=inbox: this_day_in_history(s, ib, years_ago=5, limit=3),
        ),
        # Per-inbox subsystem discoverability widget. One
        # warm target per inbox: the cache key is limit-less
        # since v1.19.3, so every caller (front-page top-12,
        # inbox dashboard top-10, cross-inbox aggregator)
        # slices from the same cached top-100 payload.
        (
            f"{inbox.name} most_active_subsystems_in_inbox (7d)",
            lambda s, ib=inbox: most_active_subsystems_in_inbox(s, ib, days=7),
        ),
        # Per-subsystem dashboard helpers, top-N most active
        # subsystems only. Coarse-grained: one warm target per
        # inbox covering 4 helpers × top-N subsystems internally.
        # Long-tail subsystems pay one cold load per hour.
        (
            f"{inbox.name} subsystem dashboards (top {WARM_TOP_SUBSYSTEMS_PER_INBOX})",
            lambda s, ib=inbox: _warm_subsystem_dashboards(
                s,
                ib,
                WARM_TOP_SUBSYSTEMS_PER_INBOX,
            ),
        ),
    ]
    for label, substr in (inbox.tracked_authors or {}).items():
        # Dashboard tracker tile (limit=5) AND per-author atom
        # feed (limit=FEED_ENTRY_LIMIT) hit different cache
        # keys; warm both so the first feed poll per hour
        # gets a cache-hit just like the dashboard.
        targets.append(
            (
                f"{inbox.name} tracker:{label}",
                lambda s, ib=inbox, sub=substr: author_recent(s, ib, sub, 5),
            )
        )
        targets.append(
            (
                f"{inbox.name} tracker:{label} (feed)",
                lambda s, ib=inbox, sub=substr: author_recent(
                    s,
                    ib,
                    sub,
                    FEED_ENTRY_LIMIT,
                ),
            )
        )
    return targets


def _build_inbox_targets(
    inbox: Inbox,
    today: "datetime.date",
    yesterday: "datetime.date",
    sitemap_base: str = "",
) -> list[tuple[str, "object"]]:
    """Legacy combined builder: fast + slow concatenated. Kept so
    `mimir warm-cache --tier all` (the operator one-off) and any
    callers that haven't been migrated still get the full target
    list. New scheduler-side callers use _build_fast_inbox_targets /
    _build_slow_inbox_targets directly.

    Order differs slightly from the pre-split shape (fast comes
    first now), but no existing test pins ordering of this list,
    only set-membership.
    """
    return _build_fast_inbox_targets(inbox, sitemap_base) + _build_slow_inbox_targets(
        inbox, today, yesterday
    )


def _build_fast_global_targets(
    sitemap_base: str = "",
) -> list[tuple[str, "object"]]:
    """Fast tier global warm targets: sitemap:index + sitemap:meta
    only, and only when sitemap_base is non-empty. Sub-100 ms each;
    designed to fire on the per-minute scheduler tick alongside the
    fast per-inbox targets so crawler-facing surfaces stay within a
    minute of fresh.

    Empty list when sitemap_base is unset: no other fast-tier global
    surfaces exist today, so an unconfigured site_base_url means the
    fast-tier global pass has nothing to do."""
    targets: list[tuple[str, object]] = []
    if sitemap_base:
        targets.append(
            (
                "sitemap:index",
                lambda s, base=sitemap_base: sitemap_index_xml(s, base),
            )
        )
        targets.append(
            (
                "sitemap:meta",
                lambda s, base=sitemap_base: meta_sitemap_xml(s, base),
            )
        )
    return targets


def _build_slow_global_targets() -> list[tuple[str, "object"]]:
    """Slow tier global warm targets: just
    `most_active_subsystems_global (7d)`. The aggregator reads per-
    inbox cache rows, so it MUST run after the per-inbox slow tier
    has populated those rows (the broker shape: a separate
    `warm_global` RPC fired after the per-inbox fan-out drains)."""
    return [
        (
            "most_active_subsystems_global (7d)",
            lambda s: most_active_subsystems_global(s, days=7),
        )
    ]


def _build_global_targets(
    sitemap_base: str = "",
) -> list[tuple[str, "object"]]:
    """Legacy combined builder: fast + slow concatenated. Kept so
    `mimir warm-cache --tier all` (the operator one-off) and any
    callers that haven't been migrated still get the full target
    list. New scheduler-side callers use _build_fast_global_targets /
    _build_slow_global_targets directly.

    Order is sitemap:index, sitemap:meta, most_active_subsystems_global
    when sitemap_base is set, matching the pre-split shape (which
    inserted the sitemap targets at positions 0 and 1)."""
    return _build_fast_global_targets(sitemap_base) + _build_slow_global_targets()


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
    subs = (
        session.execute(
            select(Subsystem)
            .options(selectinload(Subsystem.paths))
            .where(Subsystem.id.in_(sub_ids))
        )
        .scalars()
        .all()
    )
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
            session,
            inbox,
            sub,
            limit=SUBSYSTEM_RECENT_PATCHES_LIMIT,
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
            session,
            inbox,
            sub,
            days=30,
            limit=10,
        ):
            reviewer_addrs.add(r.address_normalized)
    # Warm articles_reviewed_by for each unique reviewer surfaced
    # above. Arguments must match the `reviewer_view` route call site
    # in `mimir/web/routes/search.py` (limit = REVIEWS_PER_PAGE_LIMIT)
    # or the cache key diverges and the warmed row never hits.
    for addr in reviewer_addrs:
        articles_reviewed_by(
            session,
            inbox,
            addr,
            limit=REVIEWS_PER_PAGE_LIMIT,
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
@click.option(
    "--tier",
    type=click.Choice(["fast", "slow", "all"]),
    default="all",
    help=(
        "Which warm tier to refresh. 'fast' covers sitemaps + a "
        "handful of cheap front-page helpers, suitable for a "
        "per-minute scheduler tick. 'slow' covers subsystem "
        "dashboards + per-tracker + the rest, suitable for a "
        "per-hour tick. 'all' (the default) preserves today's "
        "single-tier behaviour for ad-hoc operator runs."
    ),
)
def warm_cache_command(verbose: int, workers: int | None, tier: str) -> None:
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

    inboxes = {ix.name: ix for ix in list_inboxes()}
    worker_count = workers if workers is not None else min(os.cpu_count() or 1, 8)
    total_start = time.perf_counter()

    # Per-tier target-label resolution. The flag drives `targets=`
    # filtering on each warm_inbox / warm_global RPC: fast = the
    # cheap helpers the per-minute scheduler tick refreshes; slow =
    # the heavy subsystem dashboards + tracker + per-day reads the
    # per-hour tick refreshes; all = no targets= filter, broker
    # handler runs the full target list (today's shape, back-compat
    # for operator ad-hoc invocations). Labels are extracted from
    # the (label, fn) tuples produced by the per-tier builders;
    # only the labels matter on the CLI side because the broker
    # handler re-derives the callables from the inbox name.
    sitemap_base = settings.site_base_url or ""
    if tier == "fast":

        def per_inbox_targets(inbox):
            return [
                label
                for label, _ in _build_fast_inbox_targets(
                    inbox, sitemap_base=sitemap_base
                )
            ]

        global_targets: list[str] | None = [
            label for label, _ in _build_fast_global_targets(sitemap_base=sitemap_base)
        ]
    elif tier == "slow":
        # threads_for_day's cache key includes the date; compute
        # today / yesterday once so both per-inbox labels carry
        # the same cycle's dates.
        today = date.today()
        yesterday = today - timedelta(days=1)

        def per_inbox_targets(inbox):
            return [
                label for label, _ in _build_slow_inbox_targets(inbox, today, yesterday)
            ]

        global_targets = [label for label, _ in _build_slow_global_targets()]
    else:  # "all"

        def per_inbox_targets(inbox):
            return None  # signal "full list" to the broker handler

        global_targets = None

    # Fan out warm_inbox RPCs in parallel, then fire warm_global
    # once the per-inbox fan-out drains. The broker's N warm-workers
    # chew through the jobs concurrently. The CLI-side ThreadPool is
    # just a fan-out + collect pattern; no work runs in this process
    # beyond JSON encode/decode.
    from mimir.broker.client import BrokerUnavailable, get_broker_client

    client = get_broker_client()
    total_keys = 0
    try:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {
                pool.submit(
                    client.warm_inbox, name, targets=per_inbox_targets(inbox)
                ): name
                for name, inbox in inboxes.items()
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
                        f"{name}: {len(warmed)} keys warmed in {elapsed_ms} ms"
                        + (f" (errors: {len(errors)})" if errors else "")
                    )
        # Global Phase B after the per-inbox fan-out drains.
        # Only pass `targets=` when a non-None list narrows the
        # dispatch; otherwise call with no kwargs to stay compatible
        # with `BrokerClient.warm_global`'s current signature (the
        # `targets=` kwarg on the broker side lands in a later task,
        # see Task 5).
        if global_targets is not None:
            global_result = client.warm_global(targets=global_targets)
        else:
            global_result = client.warm_global()
        total_keys += len(global_result.get("warmed", []))
        if verbose:
            click.echo(
                f"warm_global: {len(global_result.get('warmed', []))} "
                f"keys warmed in {global_result.get('elapsed_ms', 0)} ms"
            )
    except BrokerUnavailable as exc:
        raise click.ClickException(f"broker warm-cache failed: {exc}")
    total_ms = (time.perf_counter() - total_start) * 1000
    click.echo(
        f"warm-cache: {len(inboxes)} inbox{'' if len(inboxes) == 1 else 'es'}, "
        f"{total_keys} keys, {total_ms:.0f} ms total"
    )
    purged = cache_mod.purge_expired()
    if purged:
        click.echo(f"purged {purged} expired cache row{'' if purged == 1 else 's'}")
