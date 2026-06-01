"""Tests for the warm-cache refresh-window TTL-zone classifier.

The classifier is a pure function: given `nominal_ttl`,
`window_sec`, and `elapsed`, it returns a `Decision` enum
indicating whether the warm-cache tick should skip, refresh
probabilistically, or refresh deterministically. The probability
math lives in a sibling `should_refresh` function so the
classifier itself stays deterministic and trivially testable."""

from unittest.mock import patch

from mimir.cache_warm import (
    Decision,
    classify_zone,
    effective_window_sec,
    should_refresh,
    stored_ttl_for,
)


def test_effective_window_sec_uncapped_for_long_ttl():
    """1h nominal + 10min window: window fits inside half-nominal,
    no cap applies, effective window equals configured window."""
    assert effective_window_sec(nominal_ttl=3600, window_sec=600) == 600


def test_effective_window_sec_capped_for_short_ttl():
    """5min nominal + 10min configured window: window can't exceed
    nominal // 2 = 150s, so effective window is 150s."""
    assert effective_window_sec(nominal_ttl=300, window_sec=600) == 150


def test_effective_window_sec_zero_for_sub_2sec_nominal():
    """Sub-2-second nominal: cap floors window to 0 (no window,
    identical to today's TTL-skip without the refresh-window
    mechanic). Guards the degenerate case."""
    assert effective_window_sec(nominal_ttl=1, window_sec=600) == 0


def test_stored_ttl_for_extends_by_effective_window():
    """stored_ttl = nominal_ttl + effective_window_sec(nominal_ttl,
    window_sec). For sitemap (nominal=3600, window=600): stored=4200."""
    assert stored_ttl_for(nominal_ttl=3600, window_sec=600) == 4200
    # Short-TTL cap takes effect:
    assert stored_ttl_for(nominal_ttl=300, window_sec=600) == 450


def test_classify_zone_skip_when_remaining_above_2x_window():
    """Outside the refresh window: remaining > 2 * effective_window.
    For sitemap example (stored=4200, window=600): elapsed < 3000
    means remaining > 1200 → skip."""
    decision = classify_zone(stored_ttl=4200, window_sec=600, elapsed=2999)
    assert decision == Decision.SKIP


def test_classify_zone_probabilistic_in_upper_window():
    """Probabilistic zone: window_sec < remaining <= 2 * window.
    For sitemap example: elapsed in [3000, 3600) means remaining
    in (600, 1200] → PROBABILISTIC."""
    assert (
        classify_zone(stored_ttl=4200, window_sec=600, elapsed=3000)
        == Decision.PROBABILISTIC
    )
    assert (
        classify_zone(stored_ttl=4200, window_sec=600, elapsed=3599)
        == Decision.PROBABILISTIC
    )


def test_classify_zone_deterministic_in_insurance_band():
    """Deterministic insurance: remaining <= window_sec. For sitemap
    example: elapsed in [3600, 4200) means remaining <= 600
    → DETERMINISTIC."""
    assert (
        classify_zone(stored_ttl=4200, window_sec=600, elapsed=3600)
        == Decision.DETERMINISTIC
    )
    assert (
        classify_zone(stored_ttl=4200, window_sec=600, elapsed=4199)
        == Decision.DETERMINISTIC
    )


def test_should_refresh_skip_returns_false():
    """Outside the window: never refresh."""
    assert should_refresh(stored_ttl=4200, window_sec=600, elapsed=1000) is False


def test_should_refresh_deterministic_returns_true():
    """In insurance band: always refresh."""
    assert should_refresh(stored_ttl=4200, window_sec=600, elapsed=3800) is True


def test_should_refresh_probabilistic_ramp_at_window_start():
    """At the very start of the probabilistic zone (remaining =
    2 * window): p = 0, so even random()=0 doesn't fire."""
    with patch("mimir.cache_warm.random.random", return_value=0.0):
        # Elapsed=3000: remaining=1200, p = 1 - (1200 - 600) / 600 = 0
        assert should_refresh(stored_ttl=4200, window_sec=600, elapsed=3000) is False


def test_should_refresh_probabilistic_ramp_at_window_end():
    """At the very end of the probabilistic zone (remaining = window
    + 1s): p ≈ 1, so even random()≈1 fires."""
    with patch("mimir.cache_warm.random.random", return_value=0.99):
        # Elapsed=3599: remaining=601, p = 1 - (601 - 600) / 600 ≈ 0.998
        assert should_refresh(stored_ttl=4200, window_sec=600, elapsed=3599) is True


def test_should_refresh_probabilistic_ramp_midpoint():
    """At the probabilistic-zone midpoint (remaining = 1.5 * window):
    p = 0.5. random()=0.4 fires; random()=0.6 doesn't."""
    midpoint_elapsed = 3300  # remaining=900, p = 1 - (900-600)/600 = 0.5
    with patch("mimir.cache_warm.random.random", return_value=0.4):
        assert (
            should_refresh(stored_ttl=4200, window_sec=600, elapsed=midpoint_elapsed)
            is True
        )
    with patch("mimir.cache_warm.random.random", return_value=0.6):
        assert (
            should_refresh(stored_ttl=4200, window_sec=600, elapsed=midpoint_elapsed)
            is False
        )


def test_should_refresh_monte_carlo_fires_before_expiry():
    """1000 trials of stepping a sitemap key through its stored TTL
    one tick at a time. Refresh fires before expiry in 100% of
    trials because the deterministic insurance zone catches any
    that the probabilistic ramp missed."""
    fire_count = 0
    trials = 1000
    cadence_sec = 60  # fast-tier tick rate
    for _ in range(trials):
        fired = False
        for elapsed in range(0, 4200, cadence_sec):
            if should_refresh(stored_ttl=4200, window_sec=600, elapsed=elapsed):
                fired = True
                break
        if fired:
            fire_count += 1
    assert fire_count == trials, (
        f"refresh failed to fire before expiry in "
        f"{trials - fire_count}/{trials} trials; "
        f"deterministic insurance zone should catch all"
    )
