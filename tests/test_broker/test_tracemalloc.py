"""Tests for the broker tracemalloc snapshotter.

See _claude/specs/2026-06-11-broker-tracemalloc-diagnostic-design.md.
"""

import threading
import time
import tracemalloc
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


def test_snapshotter_writes_files_at_interval(tmp_path):
    """interval=1s, 3s wait: at least 2 .pkl files land and load cleanly."""
    diag = tmp_path / "diag"
    _maybe_start_tracemalloc_snapshotter(interval=1, diagnostics_dir=diag, frames=10)
    try:
        time.sleep(3.2)
        pkls = sorted(diag.glob("tracemalloc-*.pkl"))
        assert len(pkls) >= 2, f"expected >= 2 snapshots, got {len(pkls)}"
        # Each .pkl must be a loadable tracemalloc Snapshot
        for p in pkls:
            snap = tracemalloc.Snapshot.load(str(p))
            assert isinstance(snap, tracemalloc.Snapshot)
        # No partial .tmp files left over on the happy path
        tmps = list(diag.glob("*.tmp"))
        assert tmps == []
    finally:
        tracemalloc.stop()
