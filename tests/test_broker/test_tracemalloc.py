"""Tests for the broker tracemalloc snapshotter.

See _claude/specs/2026-06-11-broker-tracemalloc-diagnostic-design.md.
"""

import logging
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


def test_snapshotter_aborts_when_diagnostics_dir_unwritable(tmp_path, caplog):
    """mkdir failure logs ERROR and aborts without starting tracemalloc."""
    # Create a regular file at the directory path so mkdir's
    # exist_ok=True can't recover; OSError raised.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    diag = blocker / "diag"  # mkdir will fail: parent is a file
    # Snapshot threads before the call so we can assert no new thread spawned
    # (a daemon thread from an earlier test may still be alive but winding down).
    before = set(t.ident for t in _live_threads_named("broker-tracemalloc"))
    with caplog.at_level(logging.ERROR, logger="mimir.broker.server"):
        with patch("tracemalloc.start") as mock_start:
            _maybe_start_tracemalloc_snapshotter(interval=10, diagnostics_dir=diag)
    after = _live_threads_named("broker-tracemalloc")
    assert not mock_start.called
    # No new thread should have been spawned by this call
    assert all(t.ident in before for t in after), (
        f"unexpected new broker-tracemalloc thread(s) started by failed call: "
        f"{[t for t in after if t.ident not in before]}"
    )
    assert any("tracemalloc snapshotter disabled" in r.message for r in caplog.records)


def test_snapshotter_survives_write_failure(tmp_path, caplog):
    """Read-only diagnostics_dir mid-loop: warning logged, loop continues."""
    diag = tmp_path / "diag"
    diag.mkdir()
    with caplog.at_level(logging.WARNING, logger="mimir.broker.server"):
        _maybe_start_tracemalloc_snapshotter(
            interval=1, diagnostics_dir=diag, frames=10
        )
    try:
        # First snapshot lands while writable
        time.sleep(1.5)
        first = sorted(diag.glob("tracemalloc-*.pkl"))
        assert len(first) >= 1
        # Make the dir read-only; next iteration will fail to write
        diag.chmod(0o555)
        time.sleep(2.0)
        # Thread is still alive
        assert _live_threads_named("broker-tracemalloc")
        # A warning was logged
        assert any("tracemalloc snapshot failed" in r.message for r in caplog.records)
        # Restore writability; next iteration writes a fresh file
        diag.chmod(0o755)
        time.sleep(2.0)
        after = sorted(diag.glob("tracemalloc-*.pkl"))
        assert len(after) > len(first)
    finally:
        diag.chmod(0o755)
        tracemalloc.stop()
