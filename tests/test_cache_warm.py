"""Unit tests for `mimir.cache_warm`.

`classify_zone` is a pure function: given `nominal_ttl`,
`window_sec`, and `remaining`, it returns a `Decision` enum
(SKIP / PROBABILISTIC / DETERMINISTIC). The probabilistic ramp
math lives in a sibling `should_refresh` function so the
classifier stays deterministic and testable.
"""

from unittest.mock import patch

from mimir.cache_warm import (
    Decision,
    classify_zone,
    effective_window_sec,
    should_refresh,
)


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


def test_classify_zone_skip_when_remaining_above_2x_window():
    """remaining=1201 > 2 * 600 -> SKIP."""
    decision = classify_zone(nominal_ttl=3600, window_sec=600, remaining=1201)
    assert decision == Decision.SKIP


def test_classify_zone_probabilistic_in_upper_window():
    """600 < remaining <= 1200 -> PROBABILISTIC, at both ends of the
    band."""
    assert (
        classify_zone(nominal_ttl=3600, window_sec=600, remaining=1200)
        == Decision.PROBABILISTIC
    )
    assert (
        classify_zone(nominal_ttl=3600, window_sec=600, remaining=601)
        == Decision.PROBABILISTIC
    )


def test_classify_zone_deterministic_in_insurance_band():
    """remaining <= 600 -> DETERMINISTIC, at both ends of the band."""
    assert (
        classify_zone(nominal_ttl=3600, window_sec=600, remaining=600)
        == Decision.DETERMINISTIC
    )
    assert (
        classify_zone(nominal_ttl=3600, window_sec=600, remaining=1)
        == Decision.DETERMINISTIC
    )


def test_classify_zone_uses_the_capped_window_for_short_ttl():
    """nominal_ttl=300 caps the window at 150, so the bands sit at
    150 and 300, not at window_sec. Working the window back out of
    the stored ttl got this wrong: warm-cache stores nominal +
    window_sec uncapped (900 here), and the recovery then returned
    300, firing for a row with 400s left that the spec says to skip."""
    assert (
        classify_zone(nominal_ttl=300, window_sec=600, remaining=400) == Decision.SKIP
    )
    assert (
        classify_zone(nominal_ttl=300, window_sec=600, remaining=300)
        == Decision.PROBABILISTIC
    )
    assert (
        classify_zone(nominal_ttl=300, window_sec=600, remaining=150)
        == Decision.DETERMINISTIC
    )


def test_should_refresh_skip_returns_false():
    """SKIP zone -> never refresh."""
    assert should_refresh(nominal_ttl=3600, window_sec=600, remaining=3200) is False


def test_should_refresh_deterministic_returns_true():
    """DETERMINISTIC zone -> always refresh."""
    assert should_refresh(nominal_ttl=3600, window_sec=600, remaining=400) is True


def test_should_refresh_probabilistic_ramp_at_window_start():
    """remaining=1200 (2 * effective): p=0. Even a draw of 0.0 does
    not fire, because `0.0 < 0.0` is False."""
    with patch("mimir.cache_warm.random.random", return_value=0.0):
        assert should_refresh(nominal_ttl=3600, window_sec=600, remaining=1200) is False


def test_should_refresh_probabilistic_ramp_at_window_end():
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
