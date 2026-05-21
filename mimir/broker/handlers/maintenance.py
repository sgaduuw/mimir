"""Maintenance-op handlers (Phase 2.3): `update_mainline`,
`analyze`, `vacuum`.

Sibling to `longops.py` (ingest, backfills, bootstrap) and
`warm.py` (cache warming). Split off into its own module so the
"periodic SQLite hygiene + mainline-tree refresh" concern stays
narrow as the long-op family keeps growing; the dispatch table in
`handlers/__init__.py` re-exports the handlers uniformly.

All three handlers delegate to public functions in
`mimir.mainline` / `mimir.maintenance` rather than re-implementing
their bodies, the CLI command stays a thin click wrapper around
the same functions, and the broker handler's only job is to
translate the RPC into a Python call.

VACUUM is the load-bearing exception in this file: it acquires the
SQLite exclusive lock for the duration of the operation, which
freezes every other broker worker (cache + long + warm). The
handler emits a high-visibility WARNING at start so an operator
correlating a cache-write stall against the broker log can tell
"weekly maintenance, not a fault." Documented in CONTEXT.md."""

import logging

from mimir.broker.protocol import (
    AnalyzeRequest,
    Reply,
    UpdateMainlineRequest,
    VacuumRequest,
)

logger = logging.getLogger(__name__)


def handle_update_mainline(req: UpdateMainlineRequest) -> Reply:
    """Run the full `update_mainline` flow inside the broker process.
    Returns the structured result so the CLI can echo the same
    "loaded N subsystems" / "walked N commits" lines whether the
    op went through the broker or ran directly."""
    from mimir.mainline import update_mainline

    result = update_mainline(
        skip_fetch=req.skip_fetch,
        skip_maintainers=req.skip_maintainers,
        skip_commits=req.skip_commits,
        force=req.force,
    )
    return Reply(ok=True, result=result.model_dump(mode="json"))


def handle_analyze(req: AnalyzeRequest) -> Reply:
    """Run ANALYZE on the broker. `full=True` triggers the no-cap
    pass (the weekly safety-net); default is the cheap bounded
    pass governed by `Settings.analyze_limit`."""
    from mimir.maintenance import run_analyze

    result = run_analyze(full=req.full)
    return Reply(ok=True, result=result.model_dump(mode="json"))


def handle_vacuum(req: VacuumRequest) -> Reply:
    """Run VACUUM on the broker.

    Holds the SQLite exclusive lock for the duration; every other
    broker worker pauses, and cache writes from the web tier queue
    behind the lock until either the VACUUM completes or the
    client's per-RPC timeout expires (matching today's direct-CLI
    VACUUM contract).

    The WARNING is the operator-visible "this is the weekly
    maintenance window" signal; without it, an operator who sees
    cache.set RPC timeouts during the window has to cross-correlate
    the cron schedule manually."""
    from mimir.maintenance import run_vacuum

    logger.warning(
        "broker: pausing for VACUUM; cache writes may time out for "
        "the duration of the operation",
    )
    result = run_vacuum()
    logger.info(
        "broker: VACUUM finished in %d ms, reclaimed %d bytes",
        result.elapsed_ms,
        result.reclaimed,
    )
    return Reply(ok=True, result=result.model_dump(mode="json"))


__all__ = [
    "handle_update_mainline",
    "handle_analyze",
    "handle_vacuum",
]
