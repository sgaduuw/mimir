"""Unit tests for `mimir.cache_warm`.

`should_refresh` decides, from `nominal_ttl`, `window_sec` and the
row's `remaining` seconds, whether warm-cache recomputes it this
tick: never above twice the effective window, always inside it, and
on a linear ramp between. `effective_window_sec` caps the window at
half the nominal TTL.
"""

from unittest.mock import patch

from mimir.cache_warm import effective_window_sec, should_refresh


def test_effective_window_sec_uncapped_for_long_ttl():
    """nominal_ttl=3600, window_sec=600 -> effective=600 (no cap)."""
    assert effective_window_sec(nominal_ttl=3600, window_sec=600) == 600


def test_effective_window_sec_capped_for_short_ttl():
    """nominal_ttl=300, window_sec=600 -> effective=150 (capped at
    nominal // 2)."""
    assert effective_window_sec(nominal_ttl=300, window_sec=600) == 150


def test_effective_window_sec_zero_for_sub_2sec_nominal():
    """nominal_ttl=1 -> effective=0 (degenerate; no window)."""
    assert effective_window_sec(nominal_ttl=1, window_sec=600) == 0


def test_should_refresh_skips_above_twice_the_window():
    """remaining=1201 > 2 * 600: outer band, never fires."""
    with patch("mimir.cache_warm.random.random", return_value=0.0):
        assert should_refresh(nominal_ttl=3600, window_sec=600, remaining=1201) is False


def test_should_refresh_never_fires_without_a_window():
    """effective=0 (nominal_ttl=1) collapses every band to skip."""
    with patch("mimir.cache_warm.random.random", return_value=0.0):
        assert should_refresh(nominal_ttl=1, window_sec=600, remaining=0) is False


def test_should_refresh_uses_the_capped_window():
    """nominal_ttl=300 caps the window at 150, so remaining=400 is
    outside the bands even though it is below 2 * window_sec."""
    with patch("mimir.cache_warm.random.random", return_value=0.0):
        assert should_refresh(nominal_ttl=300, window_sec=600, remaining=400) is False
        assert should_refresh(nominal_ttl=300, window_sec=600, remaining=150) is True


def test_should_refresh_deterministic_inside_the_window():
    """remaining <= effective: insurance band, fires regardless of the
    random draw."""
    with patch("mimir.cache_warm.random.random", return_value=0.99):
        assert should_refresh(nominal_ttl=3600, window_sec=600, remaining=600) is True
        assert should_refresh(nominal_ttl=3600, window_sec=600, remaining=1) is True


def test_should_refresh_probabilistic_ramp_at_band_start():
    """remaining=1200 (2 * effective): p=0. Even a draw of 0.0 does
    not fire, because `0.0 < 0.0` is False."""
    with patch("mimir.cache_warm.random.random", return_value=0.0):
        assert should_refresh(nominal_ttl=3600, window_sec=600, remaining=1200) is False


def test_should_refresh_probabilistic_ramp_at_band_end():
    """remaining=601 (just above effective): p ~= 1. A draw of 0.99
    fires."""
    with patch("mimir.cache_warm.random.random", return_value=0.99):
        assert should_refresh(nominal_ttl=3600, window_sec=600, remaining=601) is True


def test_should_refresh_probabilistic_ramp_midpoint():
    """remaining=900 (midpoint of [600, 1200]): p=0.5. A draw of 0.4
    fires, a draw of 0.6 does not."""
    with patch("mimir.cache_warm.random.random", return_value=0.4):
        assert should_refresh(nominal_ttl=3600, window_sec=600, remaining=900) is True
    with patch("mimir.cache_warm.random.random", return_value=0.6):
        assert should_refresh(nominal_ttl=3600, window_sec=600, remaining=900) is False


def test_should_refresh_monte_carlo_fires_before_expiry():
    """Walking one key from the start of the probabilistic band to the
    end of the insurance band, with the real RNG, must fire at least
    once: the insurance band guarantees it even if every probabilistic
    draw misses."""
    fired = False
    for remaining in range(1200, 0, -1):
        if should_refresh(nominal_ttl=3600, window_sec=600, remaining=remaining):
            fired = True
            break
    assert fired, "no refresh fired across the whole window"
