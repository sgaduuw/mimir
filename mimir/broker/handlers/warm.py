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
)
from mimir.cache import refresh_window
from mimir.config import settings

logger = logging.getLogger(__name__)


# Refresh window the broker applies for the duration of a warm
# cycle. Same value as the CLI's `WARM_CACHE_REFRESH_WITHIN_SEC`
# (450 s, set in `mimir.cli.cache`): a key with less remaining TTL
# than this gets recomputed; one with more is left alone. Tests
# can monkey-patch this at the module level.
WARM_REFRESH_WITHIN_SEC = 450


def _run_targets(
    targets,
) -> tuple[list[str], list[str], int, list[tuple[str, int]]]:
    """Run a list of `(label, fn(session))` warm targets serially on
    a fresh session under `refresh_window(WARM_REFRESH_WITHIN_SEC)`.
    Returns `(warmed, errors, elapsed_ms, per_target)`, where
    `per_target` is `[(label, ms), ...]` sorted desc by ms.

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
    warmed: list[str] = []
    errors: list[str] = []
    per_target: list[tuple[str, int]] = []
    t0 = time.perf_counter()
    with (
        refresh_window(WARM_REFRESH_WITHIN_SEC),
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

    with _context.get_active_pool().session() as session:
        inbox = session.execute(
            select(Inbox).where(Inbox.name == req.inbox_name)
        ).scalar_one_or_none()
    if inbox is None:
        return Reply(ok=False, error=f"UnknownInbox:{req.inbox_name}")

    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    sitemap_base = (settings.site_base_url or "").rstrip("/")
    targets = _build_inbox_targets(inbox, today, yesterday, sitemap_base)
    if req.targets is not None:
        wanted = set(req.targets)
        targets = [(label, fn) for label, fn in targets if label in wanted]
    warmed, errors, elapsed_ms, per_target = _run_targets(targets)
    _log_slow_breakdown("warm_inbox", req.inbox_name, elapsed_ms, per_target)
    return Reply(
        ok=True,
        result={
            "warmed": warmed,
            "elapsed_ms": elapsed_ms,
            "errors": errors,
            "per_target": [{"label": label, "ms": ms} for label, ms in per_target],
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
    """
    from mimir.cli.cache import _build_global_targets

    sitemap_base = (settings.site_base_url or "").rstrip("/")
    targets = _build_global_targets(sitemap_base)
    warmed, errors, elapsed_ms, per_target = _run_targets(targets)
    _log_slow_breakdown("warm_global", "<global>", elapsed_ms, per_target)
    return Reply(
        ok=True,
        result={
            "warmed": warmed,
            "elapsed_ms": elapsed_ms,
            "errors": errors,
            "per_target": [{"label": label, "ms": ms} for label, ms in per_target],
        },
    )
