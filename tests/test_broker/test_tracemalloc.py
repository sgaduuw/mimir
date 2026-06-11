"""Tests for the broker tracemalloc snapshotter.

See _claude/specs/2026-06-11-broker-tracemalloc-diagnostic-design.md.
"""

import threading
from unittest.mock import patch

from mimir.broker.server import _maybe_start_tracemalloc_snapshotter


def _live_threads_named(prefix: str) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(prefix)]


def test_snapshotter_disabled_when_interval_zero(tmp_path):
    """interval=0: returns immediately, no thread, no tracemalloc.start()."""
    assert not _live_threads_named("broker-tracemalloc")
    with patch("tracemalloc.start") as mock_start:
        _maybe_start_tracemalloc_snapshotter(
            interval=0, diagnostics_dir=tmp_path / "diag"
        )
    assert not mock_start.called
    assert not _live_threads_named("broker-tracemalloc")
