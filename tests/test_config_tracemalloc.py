"""Tests for the broker tracemalloc snapshotter Settings field.

See _claude/specs/2026-06-11-broker-tracemalloc-diagnostic-design.md.
"""

from mimir.config import Settings


def test_tracemalloc_interval_defaults_to_zero(monkeypatch):
    monkeypatch.delenv("TRACEMALLOC_INTERVAL_SECONDS", raising=False)
    s = Settings(secret_key="x" * 32)
    assert s.tracemalloc_interval_seconds == 0


def test_tracemalloc_interval_reads_env(monkeypatch):
    monkeypatch.setenv("TRACEMALLOC_INTERVAL_SECONDS", "1800")
    s = Settings(secret_key="x" * 32)
    assert s.tracemalloc_interval_seconds == 1800


def test_tracemalloc_interval_negative_passes_through(monkeypatch):
    monkeypatch.setenv("TRACEMALLOC_INTERVAL_SECONDS", "-1")
    s = Settings(secret_key="x" * 32)
    # Negative is parsed as-is and treated as "off" at the consumer
    # (see _maybe_start_tracemalloc_snapshotter, which checks
    # interval <= 0). No validation rejection at the Settings layer
    # so a typo doesn't crash startup.
    assert s.tracemalloc_interval_seconds == -1
