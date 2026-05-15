"""Dynamic email allowlist sourced from the parsed MAINTAINERS file.

Every `M:` (maintainer) and `R:` (reviewer) entry in
`subsystem_maintainers` contributes its address to a frozenset that
the redaction filters in `mimir.web` consult alongside the static
`Settings.email_allowlist`. Net effect: anyone the kernel tree
recognises as a maintainer or reviewer surfaces verbatim across
From lines, DCO trailer rendering, and the per-subsystem reviewer
list (with clickable links to the per-reviewer page) without
operator-side config.

Why dynamic, not static: hard-coding maintainer addresses in
config would lag the upstream tree (people change roles, addresses
get added/removed); MAINTAINERS is the authoritative source the
kernel community already maintains. Reading it gives mimir the
same recognition surface for free.

Why a separate module: three filter call sites consume this plus
the `update-mainline` invalidation hook. Lifting it out of
`mimir.subsystems` keeps that module focused on path-resolution
helpers and gives the allowlist its own tests + cache key.

Degrades gracefully when the kernel tree isn't present: an empty
`subsystem_maintainers` table → empty frozenset → only the static
allowlist applies. New deploys without `update-mainline` having
run yet (or operators who don't ingest MAINTAINERS at all) keep
the prior redaction behaviour.

`L:` (list) addresses are deliberately excluded: they're per-
subsystem list addresses, not personal contact, and have a
separate code path (`canonical.is_list_address`) for their own
purposes. Including them here would surface list addresses on
From-line redaction surfaces, where the redaction posture is
about *personal* email visibility.
"""
import logging

from sqlalchemy import distinct, func, select

from mimir import cache
from mimir.extensions import SessionLocal
from mimir.models import SubsystemMaintainer

logger = logging.getLogger(__name__)


_CACHE_KEY = "maintainer_addresses"
# 24h TTL is large because the cache is invalidated explicitly by
# `update-mainline` whenever MAINTAINERS reloads. The TTL is a
# backstop against stale state on a deploy that somehow skips the
# invalidation hook (process crash mid-update, manual SQL meddling,
# etc.).
_CACHE_TTL_SEC = 24 * 3600


def maintainer_addresses() -> frozenset[str]:
    """Lowercased addresses from every `M:` and `R:` row in
    `subsystem_maintainers`. Cached via `mimir.cache`; cross-process
    safe (the scheduler's `update-mainline` invalidation reaches the
    web tier through the shared cache table).

    Returns an empty frozenset when MAINTAINERS hasn't been parsed
    yet, the static allowlist still applies on its own.
    """
    with SessionLocal() as session:
        # Cache stores list[str] (JSON); we coerce to frozenset on
        # the way out so callers can do O(1) `addr in set` checks
        # without re-allocating per call.
        cached = cache.get_or_compute(
            session,
            _CACHE_KEY,
            _CACHE_TTL_SEC,
            _compute_addresses,
        )
    return frozenset(cached)


def _compute_addresses() -> list[str]:
    """Single SELECT DISTINCT over `subsystem_maintainers.address`
    with LOWER() applied so the cached list is already case-folded.

    Returns a sorted list, `mimir.cache`'s JSON encoder doesn't
    accept sets, and a stable order makes the cached row's
    serialised value deterministic (helps debugging and any future
    diff-based invalidation).
    """
    with SessionLocal() as session:
        rows = session.execute(
            select(distinct(func.lower(SubsystemMaintainer.address)))
        ).all()
    return sorted(addr for (addr,) in rows if addr)


def invalidate() -> None:
    """Drop the cached set. Called from the `update-mainline` flow
    after the MAINTAINERS reload has committed; the next reader
    sees a fresh recompute on the new state."""
    cache.delete(_CACHE_KEY)


__all__ = [
    "invalidate",
    "maintainer_addresses",
]
