"""Warm-cache TTL-zone classifier with probabilistic refresh window.

Pure functions for the warm-cache TTL-skip path. The module
splits a cache row's lifetime into three zones based on the row's
`remaining` seconds and the effective window derived from the
caller's `nominal_ttl` and the per-tier `window_sec` knob:

- SKIP: `remaining > 2 * effective`. Outer band; warm-cache
  skips this tick for the key.
- PROBABILISTIC: `effective < remaining <= 2 * effective`.
  Probability of firing ramps from 0 (start of band) to 1
  (end of band). Spreads refreshes across the band so 204
  sibling keys don't all refresh on the same tick.
- DETERMINISTIC: `remaining <= effective`. Insurance zone;
  refresh fires unconditionally on every tick, so any key the
  probabilistic zone missed still gets refreshed before
  expiry.

Warm-cache stores rows with `expires_at = now + nominal_ttl +
window_sec` (`cache.ttl_extension`), which is what gives the bands
room inside the cached lifetime. Read paths see no difference.

Spec: `_claude/specs/2026-06-01-warm-cache-fast-slow-tier-split-design.md` §5.2.
"""

import random
from enum import Enum, auto


class Decision(Enum):
    SKIP = auto()
    PROBABILISTIC = auto()
    DETERMINISTIC = auto()


def effective_window_sec(*, nominal_ttl: int, window_sec: int) -> int:
    """Cap the configured window at `nominal_ttl // 2` so the
    refresh window can't exceed half the nominal lifetime.
    Floors at 0 for sub-2-second nominals so the degenerate
    case (sitemap_meta with TTL=1?) collapses cleanly to "no
    window, same as today's deterministic-refresh-at-expiry".
    """
    if nominal_ttl <= 1:
        return 0
    return min(window_sec, nominal_ttl // 2)


def classify_zone(*, nominal_ttl: int, window_sec: int, remaining: int) -> Decision:
    """Classify which TTL zone a cache row currently sits in."""
    effective = effective_window_sec(nominal_ttl=nominal_ttl, window_sec=window_sec)
    if effective == 0:
        # Degenerate: no window. Treat the whole TTL as SKIP zone
        # except past expiry (the caller shouldn't be here in that
        # case, but defensive).
        return Decision.SKIP
    if remaining > 2 * effective:
        return Decision.SKIP
    if remaining > effective:
        return Decision.PROBABILISTIC
    return Decision.DETERMINISTIC


def should_refresh(*, nominal_ttl: int, window_sec: int, remaining: int) -> bool:
    """p = 1 at remaining=effective, p = 0 at remaining=2*effective.
    Linear ramp across the PROBABILISTIC band."""
    zone = classify_zone(
        nominal_ttl=nominal_ttl, window_sec=window_sec, remaining=remaining
    )
    if zone is Decision.SKIP:
        return False
    if zone is Decision.DETERMINISTIC:
        return True
    effective = effective_window_sec(nominal_ttl=nominal_ttl, window_sec=window_sec)
    # p = 1 at remaining = effective (band end), p = 0 at remaining =
    # 2 * effective (band start). Linear ramp.
    p = 1.0 - ((remaining - effective) / effective)
    return random.random() < p
