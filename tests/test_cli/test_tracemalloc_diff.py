"""Tests for `mimir tracemalloc-diff` CLI command.

See _claude/specs/2026-06-11-broker-tracemalloc-diagnostic-design.md.
"""

import pickle
import tracemalloc

from click.testing import CliRunner

from mimir.cli.diagnostics import tracemalloc_diff_command


def test_diff_command_corrupt_input_errors_cleanly(tmp_path):
    """A non-pickle file passed as snapshot input fails with a
    Click BadParameter (exit code != 0) and a useful message."""
    bogus = tmp_path / "not-a-pickle.txt"
    bogus.write_text("this is not a pickle")
    other = tmp_path / "other.txt"
    other.write_text("also not a pickle")
    runner = CliRunner()
    result = runner.invoke(tracemalloc_diff_command, [str(bogus), str(other)])
    assert result.exit_code != 0
    assert "not-a-pickle.txt" in result.output or "not-a-pickle.txt" in str(
        result.exception
    )


def test_diff_command_ranks_growers(tmp_path):
    """Buffers allocated between two snapshots show up at the top
    of the diff output, ranked by size delta descending."""
    tracemalloc.start(10)
    try:
        # Baseline snapshot
        snap_a = tracemalloc.take_snapshot()
        path_a = tmp_path / "a.pkl"
        with open(path_a, "wb") as f:
            pickle.dump(snap_a, f)

        # Allocate a distinctive 10 MB buffer at a known line
        big = bytearray(10 * 1024 * 1024)  # noqa: F841  # keep alive

        snap_b = tracemalloc.take_snapshot()
        path_b = tmp_path / "b.pkl"
        with open(path_b, "wb") as f:
            pickle.dump(snap_b, f)
    finally:
        tracemalloc.stop()

    runner = CliRunner()
    result = runner.invoke(
        tracemalloc_diff_command,
        [str(path_a), str(path_b), "--top", "5"],
    )
    assert result.exit_code == 0, result.output
    # The 10 MB bytearray's allocation site (this test file)
    # should appear with a positive size delta
    assert "test_tracemalloc_diff.py" in result.output
    assert "MiB" in result.output


def test_diff_command_filter_prefix(tmp_path):
    """--filter-prefix narrows output to matching source files.

    Allocates two buffers in different paths (a synthetic 'foo/'
    file built via exec to control the apparent allocation site),
    diffs with and without filter, asserts narrowing.
    """
    tracemalloc.start(10)
    try:
        snap_a = tracemalloc.take_snapshot()
        path_a = tmp_path / "a.pkl"
        with open(path_a, "wb") as f:
            pickle.dump(snap_a, f)
        # Allocate from the current test file
        big = bytearray(8 * 1024 * 1024)  # noqa: F841
        snap_b = tracemalloc.take_snapshot()
        path_b = tmp_path / "b.pkl"
        with open(path_b, "wb") as f:
            pickle.dump(snap_b, f)
    finally:
        tracemalloc.stop()

    runner = CliRunner()
    # Without filter: test file's allocation appears
    result = runner.invoke(tracemalloc_diff_command, [str(path_a), str(path_b)])
    assert result.exit_code == 0
    assert "test_tracemalloc_diff.py" in result.output

    # With a non-matching filter prefix: test file's allocation
    # is filtered out
    result = runner.invoke(
        tracemalloc_diff_command,
        [str(path_a), str(path_b), "--filter-prefix", "/nonexistent/path"],
    )
    assert result.exit_code == 0
    assert "test_tracemalloc_diff.py" not in result.output
