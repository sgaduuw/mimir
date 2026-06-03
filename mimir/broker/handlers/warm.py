"""Warm-queue handlers (Phase 2.2).

Each handler runs one per-inbox or one global warm cycle inside the
broker process, against the broker's own SessionLocal. The cache
writes the warming helpers issue land via `cache.set`, which from
inside the broker process bypasses the self-RPC (see
`_should_dispatch_to_broker` in `mimir.cache`) and calls the
`_direct_set` path. The broker's cache worker is therefore NOT
involved in warming, only the warm-workers are.

Per-target exceptions are captured into the reply's `errors` list
rather than failing the whole RPC. The warming surface is best-
effort: a broken `daily_volume` helper for one inbox shouldn't
take down the scheduler's whole warm cycle and force a hotfix in
the middle of an incident.

Imports for `mimir.cli.cache` (which pulls in dashboard helpers,
subsystem aggregators, etc.) are deferred into the handler bodies
so the broker process's import-time graph stays lean, matching the
pattern used by the long-op handlers.
"""

import logging
import time

from mimir.broker import _context
from mimir.broker.protocol import (
    Reply,
    WarmGlobalRequest,
    WarmInboxRequest,
    WarmSubsystemRequest,
)
from mimir.cache import refresh_window, ttl_extension
from mimir.config import settings

logger = logging.getLogger(__name__)


# Back-compat constant retained for callers that invoke `_run_targets`
# without a `priority`. Task 5 of the fast/slow tier split moved the
# in-handler default to tier-aware sourcing from `settings.warm_cache_
# {fast,slow}_refresh_window_sec` (see `_run_targets`); the handlers
# always pass `priority=req.priority` so this fallback is only ever
# hit by ad-hoc / test callers that omit the kwarg. Kept module-level
# so tests can monkey-patch.
WARM_REFRESH_WITHIN_SEC = 450


# Config-drift guard (Layer 2): once-per-process flag for the
# sitemap-labels-but-no-SITE_BASE_URL WARNING. A misconfigured broker
# sees ~200 warm RPCs per scheduler fast-tier tick, each of which
# would otherwise log the same WARNING; gating on a module-level flag
# keeps the signal visible without flooding the log. Resets on broker
# restart (the operator who fixes the env will restart anyway, so the
# warning fires once on the next misconfigured boot if it persists).
# Tests reset this between cases via monkeypatch.
_SITEMAP_GAP_LOGGED: bool = False


def _maybe_warn_sitemap_targets_dropped(req_targets) -> None:
    """One-shot WARNING when an RPC asks for sitemap labels but the
    broker's own SITE_BASE_URL is unset. Paired with Layer 1's
    startup WARNING: that one fires unconditionally at boot, this one
    fires when the misconfig actually drops requested work (i.e. the
    scheduler started routing sitemap warm RPCs into a broker that
    can't serve them).

    Guards:
    - `req_targets is None` means "every target", which lets the
      local target builder skip sitemap entries silently. No warning
      needed there; the count discrepancy is implicit and Layer 1's
      startup warning already named the affected feature.
    - No sitemap: labels in req_targets → nothing to warn about.
    - SITE_BASE_URL set → no drift; happy path.
    - Already logged once → silent (the misconfig persists for the
      lifetime of this process; one log line per boot is enough).
    """
    global _SITEMAP_GAP_LOGGED
    if _SITEMAP_GAP_LOGGED:
        return
    if req_targets is None:
        return
    if not any(t.startswith("sitemap:") for t in req_targets):
        return
    if (settings.site_base_url or "").strip():
        return
    logger.warning(
        "broker warm handler: sitemap labels requested in this RPC "
        "(targets=[%s]) but SITE_BASE_URL is unset on the broker; "
        "sitemap rows will not be refreshed. Set SITE_BASE_URL in the "
        "broker container's environment. Suppressing further warnings "
        "for the lifetime of this process.",
        ", ".join(t for t in req_targets if t.startswith("sitemap:")),
    )
    _SITEMAP_GAP_LOGGED = True


def _run_targets(
    targets,
    *,
    priority: int = 1,
) -> tuple[list[str], list[str], int, list[tuple[str, int]]]:
    """Run a list of `(label, fn(session))` warm targets serially on
    a fresh session under tier-aware `refresh_window` +
    `ttl_extension` context. Returns
    `(warmed, errors, elapsed_ms, per_target)`, where `per_target`
    is `[(label, ms), ...]` sorted desc by ms.

    `priority` selects the tier window from settings (Task 5 of the
    fast/slow tier split, spec §5):

    - `priority=0` (fast): `settings.warm_cache_fast_refresh_window_sec`
      (default 600 s; matches the per-minute scheduler cadence with
      headroom for the probabilistic refresh ramp).
    - `priority=1` (slow): `settings.warm_cache_slow_refresh_window_sec`
      (default 7200 s; matches the per-hour cadence with enough ticks
      in the window for the ramp to fire).

    The same window value drives both the read-side `refresh_window`
    (recompute any row with less than `window` TTL remaining) AND the
    write-side `ttl_extension` (store `expires_at = now + nominal +
    window` so the deterministic insurance band sits past nominal).
    Spec §5.2 walks through why these two sides must agree.

    Per-target exceptions go into `errors` as `f"{label}: {exc!r}"`
    so the broker's reply can report partial outcomes; the
    surrounding RPC stays ok=True (best-effort warm). The single
    session per RPC matches the CLI's per-thread session shape;
    the warm-worker can hold this session for the duration of one
    inbox's warm without contending with other warm-workers
    (each worker has its own RPC and therefore its own session).

    Per-target timings ride along on every reply so the operator
    can attribute slow `warm_inbox` RPCs to a specific helper
    (e.g. `active_threads` vs the per-inbox subsystem-dashboard
    fan-out) without needing a separate `-v` repro.
    """
    if priority == 0:
        window_sec = settings.warm_cache_fast_refresh_window_sec
    else:
        window_sec = settings.warm_cache_slow_refresh_window_sec
    warmed: list[str] = []
    errors: list[str] = []
    per_target: list[tuple[str, int]] = []
    t0 = time.perf_counter()
    with (
        refresh_window(window_sec),
        ttl_extension(window_sec),
        _context.get_active_pool().session() as session,
    ):
        for label, fn in targets:
            t_target = time.perf_counter()
            try:
                fn(session)
                warmed.append(label)
            except Exception as exc:
                logger.warning(
                    "broker warm: target %r failed: %r",
                    label,
                    exc,
                )
                errors.append(f"{label}: {exc!r}")
            finally:
                per_target.append((label, int((time.perf_counter() - t_target) * 1000)))
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    per_target.sort(key=lambda lm: lm[1], reverse=True)
    return warmed, errors, elapsed_ms, per_target


def _log_slow_breakdown(op: str, tag: str, elapsed_ms: int, per_target) -> None:
    """Emit a WARNING listing the top-5 slowest targets when the
    handler's own elapsed time crosses `broker_slow_rpc_warn_ms`.
    Pairs with the server's existing `broker slow rpc` line: the
    server tells you *that* the RPC was slow, this tells you
    *which targets ate the budget*.
    """
    threshold = settings.broker_slow_rpc_warn_ms
    if threshold <= 0 or elapsed_ms < threshold:
        return
    top = per_target[:5]
    breakdown = ", ".join(f"{label}={ms}ms" for label, ms in top)
    logger.warning(
        "broker warm slow [%s] %s: %dms total; top: %s",
        op,
        tag,
        elapsed_ms,
        breakdown,
    )


def handle_warm_inbox(req: WarmInboxRequest) -> Reply:
    """Warm every cached helper for one inbox. The handler looks
    the inbox up by name (do NOT call `bootstrap_inboxes`, this is
    a read-path posture; the broker is the wrong place to seed
    config), builds the per-inbox target list, and runs the
    targets serially against this RPC's session.

    `req.targets` (when non-None) narrows the target set to the
    labelled subset, matching the post-ingest-warm posture used
    by `mimir.ingest.orchestrate._warm_after_ingest`.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from mimir.cli.cache import _build_inbox_targets
    from mimir.models import Inbox

    _maybe_warn_sitemap_targets_dropped(req.targets)

    with _context.get_active_pool().session() as session:
        inbox = session.execute(
            select(Inbox).where(Inbox.name == req.inbox_name)
        ).scalar_one_or_none()
    if inbox is None:
        return Reply(
            rpc_id=req.rpc_id, ok=False, error=f"UnknownInbox:{req.inbox_name}"
        )

    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    sitemap_base = (settings.site_base_url or "").rstrip("/")
    targets = _build_inbox_targets(inbox, today, yesterday, sitemap_base)
    if req.targets is not None:
        wanted = set(req.targets)
        targets = [(label, fn) for label, fn in targets if label in wanted]
    warmed, errors, elapsed_ms, per_target = _run_targets(
        targets, priority=req.priority
    )
    _log_slow_breakdown("warm_inbox", req.inbox_name, elapsed_ms, per_target)
    return Reply(
        rpc_id=req.rpc_id,
        ok=True,
        result={
            "warmed": warmed,
            "elapsed_ms": elapsed_ms,
            "errors": errors,
            "per_target": [{"label": label, "ms": ms} for label, ms in per_target],
        },
    )


def handle_warm_subsystem(req: WarmSubsystemRequest) -> Reply:
    """Per-(inbox, subsystem) warm. Replaces the serial inner loop of
    `_warm_subsystem_dashboards` for the case where the slow-tier CLI
    fans out one RPC per (inbox, subsystem) at the dispatch site.

    The work delegates to `mimir.cli.cache._per_subsystem_warm_call`
    so the dispatch shape stays single-source-of-truth with the
    in-handler `_warm_subsystem_dashboards` path (which calls the
    same helper). The whole sequence runs under `refresh_window` +
    `ttl_extension` per `req.priority`, matching `handle_warm_inbox`
    / `handle_warm_global` semantics.

    Production motivation 2026-06-01: linux-arm-kernel's slow-tier
    warm cycle took ~111 s wall time, ~107 s of which was the
    internal 20-subsystem dashboard loop. With 8 broker warm
    workers, per-subsystem fan-out drops this hotspot to
    ~107 s / 8 ~= 14 s.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from mimir.cli.cache import _per_subsystem_warm_call
    from mimir.models import Inbox, Subsystem

    if req.priority == 0:
        window_sec = settings.warm_cache_fast_refresh_window_sec
    else:
        window_sec = settings.warm_cache_slow_refresh_window_sec

    t0 = time.perf_counter()
    warmed: list[str] = []
    errors: list[str] = []
    sub_name_for_log: str = "?"
    with (
        refresh_window(window_sec),
        ttl_extension(window_sec),
        _context.get_active_pool().session() as session,
    ):
        inbox = session.execute(
            select(Inbox).where(Inbox.name == req.inbox_name)
        ).scalar_one_or_none()
        if inbox is None:
            return Reply(
                rpc_id=req.rpc_id, ok=False, error=f"UnknownInbox:{req.inbox_name}"
            )
        sub = session.execute(
            select(Subsystem)
            .options(selectinload(Subsystem.paths))
            .where(Subsystem.id == req.subsystem_id)
        ).scalar_one_or_none()
        if sub is None:
            return Reply(
                rpc_id=req.rpc_id,
                ok=False,
                error=f"UnknownSubsystem:{req.subsystem_id}",
            )
        sub_name_for_log = sub.name
        try:
            warmed_labels = _per_subsystem_warm_call(session, inbox, sub)
            warmed.extend(warmed_labels)
        except Exception as exc:
            logger.warning(
                "broker warm: warm_subsystem(%s, %s) failed: %r",
                req.inbox_name,
                sub.name,
                exc,
            )
            errors.append(f"{inbox.name}/{sub.name}: {exc!r}")

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if elapsed_ms >= settings.broker_slow_rpc_warn_ms:
        logger.warning(
            "broker warm slow [warm_subsystem] %s/%s: %dms total; warmed=%d errors=%d",
            req.inbox_name,
            sub_name_for_log,
            elapsed_ms,
            len(warmed),
            len(errors),
        )

    return Reply(
        rpc_id=req.rpc_id,
        ok=True,
        result={
            "warmed": warmed,
            "elapsed_ms": elapsed_ms,
            "errors": errors,
        },
    )


def handle_warm_global(req: WarmGlobalRequest) -> Reply:
    """Warm the cross-inbox aggregators (`most_active_subsystems_global`
    + sitemap index/meta when `SITE_BASE_URL` is set). Caller is
    responsible for ordering: this RPC MUST follow every
    `warm_inbox` job in the same cycle, otherwise the aggregator
    can read a stale per-inbox row and produce a global result
    that lags the freshly-warmed components.

    The CLI dispatcher in `mimir.cli.cache.warm_cache_command`
    handles this sequencing by awaiting the per-inbox fan-out
    before issuing the global RPC.

    `req.targets` (Task 5 of the fast/slow tier split) narrows the
    global aggregator set to a labelled subset, mirroring
    `handle_warm_inbox`'s targets filter. None = run every global
    aggregator.
    """
    from mimir.cli.cache import _build_global_targets

    _maybe_warn_sitemap_targets_dropped(req.targets)

    sitemap_base = (settings.site_base_url or "").rstrip("/")
    targets = _build_global_targets(sitemap_base)
    if req.targets is not None:
        wanted = set(req.targets)
        targets = [(label, fn) for label, fn in targets if label in wanted]
    warmed, errors, elapsed_ms, per_target = _run_targets(
        targets, priority=req.priority
    )
    _log_slow_breakdown("warm_global", "<global>", elapsed_ms, per_target)
    return Reply(
        rpc_id=req.rpc_id,
        ok=True,
        result={
            "warmed": warmed,
            "elapsed_ms": elapsed_ms,
            "errors": errors,
            "per_target": [{"label": label, "ms": ms} for label, ms in per_target],
        },
    )
