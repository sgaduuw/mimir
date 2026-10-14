"""Probabilistic refresh window for the warm-cache TTL-skip path.

Pure functions. A cache row's remaining lifetime falls into one of
three bands, measured against `effective = effective_window_sec(...)`:

- `remaining > 2 * effective`: warm-cache skips this tick for the key.
- `effective < remaining <= 2 * effective`: probability of firing ramps
  from 0 (start of band) to 1 (end of band), spreading refreshes
  across the band so sibling keys don't all refresh on the same tick.
- `remaining <= effective`: insurance band; refresh fires on every
  tick, so a key the probabilistic band missed still gets refreshed
  before expiry.

Warm-cache stores rows with `expires_at = now + nominal_ttl +
window_sec` (`cache.ttl_extension`), which is what gives the bands
room inside the cached lifetime. Read paths see no difference.

Spec: `_claude/specs/2026-06-01-warm-cache-fast-slow-tier-split-design.md` §5.2.
"""

import random


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


def should_refresh(*, nominal_ttl: int, window_sec: int, remaining: int) -> bool:
    """Whether warm-cache recomputes a row with `remaining` seconds
    left on the clock. See the module docstring for the bands."""
    effective = effective_window_sec(nominal_ttl=nominal_ttl, window_sec=window_sec)
    if effective == 0 or remaining > 2 * effective:
        return False
    if remaining <= effective:
        return True
    # p = 1 at remaining = effective (band end), p = 0 at remaining =
    # 2 * effective (band start). Linear ramp.
    return random.random() < 1.0 - ((remaining - effective) / effective)
