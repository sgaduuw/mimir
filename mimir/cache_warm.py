"""Warm-cache TTL-zone classifier with probabilistic refresh window.

Pure functions for the warm-cache TTL-skip path. The module
splits a cache row's lifetime into three zones based on
`elapsed = now - set_at`, `stored_ttl`, and the per-tier
`window_sec` knob:

- SKIP: `remaining > 2 * window_sec`. Outer band; warm-cache
  skips this tick for the key.
- PROBABILISTIC: `window_sec < remaining <= 2 * window_sec`.
  Probability of firing ramps from 0 (start of band) to 1
  (end of band). Spreads refreshes across the band so 204
  sibling keys don't all refresh on the same tick.
- DETERMINISTIC: `remaining <= window_sec`. Insurance zone;
  refresh fires unconditionally on every tick, so any key the
  probabilistic zone missed still gets refreshed before
  expiry.

`stored_ttl = nominal_ttl + effective_window_sec(nominal_ttl,
window_sec)`. Callers (warm-cache target builders) populate
cache rows with the stored TTL; read paths see no difference.

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


def stored_ttl_for(*, nominal_ttl: int, window_sec: int) -> int:
    """Stored TTL = nominal + effective_window. Callers pass this
    to `cache.set` when populating a warm-managed row."""
    return nominal_ttl + effective_window_sec(
        nominal_ttl=nominal_ttl, window_sec=window_sec
    )


def _effective_for_stored(*, stored_ttl: int, window_sec: int) -> int:
    """Recover effective_window from stored_ttl + window_sec.

    Exact given a fixed (nominal, window_sec) pair: the two
    algebraic cases below are deterministic given the construction
    rule in `effective_window_sec`.

    - Long-TTL case (cap didn't fire): stored = nominal + window_sec,
      so effective = window_sec.
    - Short-TTL case (cap fired, effective = nominal // 2): stored =
      nominal + nominal // 2 = 3 * nominal // 2, and effective is
      recovered from nominal ≈ stored * 2 // 3.

    The failure mode worth knowing about is config drift between
    write and read: a row written under `window_sec=600` then read
    under `window_sec=300` returns `effective=300` instead of 600,
    because recovery uses the read-time `window_sec`. Downstream
    zone classification still operates on `remaining` (invariant to
    this drift), so no row is corrupted; the only effect is shifted
    SKIP / PROBABILISTIC / DETERMINISTIC boundaries until the
    rows churn under the new config.
    """
    # Try long-TTL first: nominal = stored - window_sec.
    long_nominal = stored_ttl - window_sec
    if (
        long_nominal > 0
        and effective_window_sec(nominal_ttl=long_nominal, window_sec=window_sec)
        == window_sec
    ):
        return window_sec
    # Short-TTL branch: stored = 3 * nominal // 2, so nominal ≈
    # stored * 2 // 3. effective = nominal // 2.
    short_nominal = stored_ttl * 2 // 3
    return effective_window_sec(nominal_ttl=short_nominal, window_sec=window_sec)


def classify_zone(*, stored_ttl: int, window_sec: int, elapsed: int) -> Decision:
    """Classify which TTL zone a cache row currently sits in."""
    effective = _effective_for_stored(stored_ttl=stored_ttl, window_sec=window_sec)
    if effective == 0:
        # Degenerate: no window. Treat the whole TTL as SKIP zone
        # except past expiry (the caller shouldn't be here in that
        # case, but defensive).
        return Decision.SKIP
    remaining = stored_ttl - elapsed
    if remaining > 2 * effective:
        return Decision.SKIP
    if remaining > effective:
        return Decision.PROBABILISTIC
    return Decision.DETERMINISTIC


def should_refresh(*, stored_ttl: int, window_sec: int, elapsed: int) -> bool:
    """p = 1 at remaining=effective, p = 0 at remaining=2*effective.
    Linear ramp across the PROBABILISTIC band."""
    zone = classify_zone(stored_ttl=stored_ttl, window_sec=window_sec, elapsed=elapsed)
    if zone is Decision.SKIP:
        return False
    if zone is Decision.DETERMINISTIC:
        return True
    effective = _effective_for_stored(stored_ttl=stored_ttl, window_sec=window_sec)
    remaining = stored_ttl - elapsed
    # p = 1 at remaining = effective (band end), p = 0 at remaining =
    # 2 * effective (band start). Linear ramp.
    p = 1.0 - ((remaining - effective) / effective)
    return random.random() < p
