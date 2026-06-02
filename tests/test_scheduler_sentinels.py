"""Tests for `deploy/scheduler.sh`'s sentinel-file persistence logic.

CONTEXT.md "Scheduler-loop timers persist across restarts": each
periodic task (warm-cache fast/slow, update, update-mainline,
analyze, analyze --full, vacuum) records a `/data/.last_<task>`
sentinel touched on every successful run. The next-tick decision
reads `now - mtime(sentinel) >= cadence_seconds` from the
PERSISTED mtime (not from a per-container-restart wall clock),
so a release rollover at a frequency shorter than the slowest
task's cadence (daily ANALYZE, weekly VACUUM) doesn't silently
push the task out forever.

Two load-bearing properties that the 2026-05-20 incident (cited
in MEMORY.md) demanded:

1. **Missing sentinel returns 0** so the next tick fires
   immediately. Recovers from a fresh `/data` volume; recovers
   from a first deploy after the persistence fix landed.
2. **A failed run does NOT touch the sentinel.** A transient
   failure must NOT push the next retry out by a full cadence;
   the next tick retries on the unchanged old mtime.

Both behaviors live in pure bash inside `deploy/scheduler.sh`.
Testing them from pytest extracts the `sentinel_mtime` and `run`
function definitions out of the script at test time, runs them
under `sh` in a temp directory, and asserts on the side effects.

A future refactor that moves these helpers into a sourced
`deploy/_scheduler_helpers.sh` would simplify the tests by
letting them source the file directly; for now the extract-at-
test-time keeps PR 1.2 scope contained.
"""

import re
import subprocess
from pathlib import Path

SCHEDULER_SH = Path(__file__).resolve().parent.parent / "deploy" / "scheduler.sh"


def _extract_multiline_function(name: str, source: str) -> str:
    """Pull a multi-line bash function definition by name out of
    `source`. Matches `<name>() {` on its own line through the
    corresponding `}` at column zero. The two functions extracted
    by this test (`sentinel_mtime`, `run`) are both multi-line in
    scheduler.sh today. `log()` is a one-liner and is provided
    inline by `_scheduler_helpers_snippet()` instead, since
    extracting a one-liner with the same regex would need a
    different pattern.
    """
    pattern = re.compile(
        rf"^{re.escape(name)}\(\) \{{\n(?:.*\n)*?\}}\n",
        re.MULTILINE,
    )
    match = pattern.search(source)
    if match is None:
        raise AssertionError(
            f"could not extract function {name!r} from {SCHEDULER_SH}; "
            f"did the function name change or its formatting drift "
            f"away from the multi-line shape?"
        )
    return match.group(0)


# `log()` in scheduler.sh is a one-liner; inline it here rather
# than handle two extraction patterns. If a future refactor moves
# this to multi-line, the inline version is a harmless override.
_INLINE_LOG = 'log() { echo "[scheduler $(date -Iseconds)] $*"; }\n'


def _scheduler_helpers_snippet() -> str:
    """Build a shell snippet defining the three helpers
    (`log`, `sentinel_mtime`, `run`) used by the test cases. `log`
    is inlined; the other two are extracted from `deploy/scheduler.sh`
    at test time so a refactor of their contents is automatically
    picked up.
    """
    source = SCHEDULER_SH.read_text()
    return (
        _INLINE_LOG
        + _extract_multiline_function("sentinel_mtime", source)
        + _extract_multiline_function("run", source)
    )


def _run_sh(snippet: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Invoke `sh -c snippet`. Returns the completed process with
    stdout + stderr captured."""
    helpers = _scheduler_helpers_snippet()
    return subprocess.run(
        ["sh", "-c", helpers + "\n" + snippet],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )


def test_sentinel_mtime_missing_file_returns_zero(tmp_path):
    """A missing sentinel must read as 0 so the cadence check
    `now - 0 >= cadence_seconds` is trivially true and the next
    tick fires immediately. Recovery path on fresh /data volume.
    """
    nope = tmp_path / "nope"
    assert not nope.exists()

    result = _run_sh(f'sentinel_mtime "{nope}"')

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0", (
        f"missing sentinel must return 0 (so the cadence check fires "
        f"immediately on a fresh /data volume). Got: {result.stdout!r}"
    )


def test_sentinel_mtime_existing_file_returns_mtime_seconds(tmp_path):
    """An existing sentinel reads its mtime in unix seconds. The
    persisted mtime is what makes cadence math survive container
    restarts (#202)."""
    import os
    import time

    sentinel = tmp_path / ".last_test"
    sentinel.touch()
    # Stamp a known mtime ~600 seconds ago so the cadence math is
    # unambiguous. The integer comparison below avoids any
    # filesystem-precision wobble.
    stamp = int(time.time()) - 600
    os.utime(sentinel, (stamp, stamp))

    result = _run_sh(f'sentinel_mtime "{sentinel}"')

    assert result.returncode == 0, result.stderr
    observed = int(result.stdout.strip())
    assert observed == stamp, (
        f"expected mtime {stamp}, got {observed}. The sentinel must "
        f"report its on-disk mtime, not the current wall clock; this "
        f"is the property that makes cadence math survive a restart."
    )


def test_run_touches_sentinel_on_command_success(tmp_path):
    """`run "label" /path/to/sentinel cmd args...` touches the
    sentinel after a successful command exit. This is the success
    path: the next cadence check sees a fresh mtime and waits the
    full window before firing again.
    """
    sentinel = tmp_path / ".last_success"
    assert not sentinel.exists()

    result = _run_sh(f'run "task-ok" "{sentinel}" true')

    assert result.returncode == 0, f"{result.stdout!r} / {result.stderr!r}"
    assert sentinel.exists(), (
        "scheduler.sh's `run` must touch the sentinel after a successful "
        "command exit so the cadence math advances; otherwise the next "
        "tick fires immediately and the cadence becomes meaningless"
    )


def test_run_does_not_touch_sentinel_on_command_failure(tmp_path):
    """The 2026-05-20-shaped invariant: a FAILED run must NOT touch
    the sentinel. If it did, a transient failure would push the next
    retry out by a full cadence (and on a weekly cadence like VACUUM,
    that's a week of no retries). The cited incident in MEMORY.md
    was the symmetric shape: sentinels re-initialised on each
    restart so the cadence never expired. Both classes share the
    same property: the sentinel's mtime is the canonical record of
    `time of last SUCCESSFUL completion`, NOT of `time of last
    attempt`.

    A regression that touched the sentinel unconditionally (e.g.
    moving `touch` outside the `if "$@"; then` branch) would silently
    mask failures and stop retries.
    """
    sentinel = tmp_path / ".last_failure"
    assert not sentinel.exists()

    result = _run_sh(f'run "task-fail" "{sentinel}" false')

    # The script itself doesn't propagate the inner command's exit
    # code (it logs and continues), so result.returncode should be 0
    # (the snippet ran cleanly). What matters is the sentinel state.
    assert result.returncode == 0, f"{result.stdout!r} / {result.stderr!r}"
    assert not sentinel.exists(), (
        "scheduler.sh's `run` must NOT touch the sentinel after a "
        "FAILED command exit. Touching it would mask the failure and "
        "push the next retry out by a full cadence; the next tick "
        "must retry on the unchanged old mtime instead. This is the "
        "exact property whose absence caused the 2026-05-20 incident "
        "(MEMORY.md): daily ANALYZE silently stopped firing for 8 days."
    )


def test_extracted_function_names_match_scheduler_sh():
    """Defensive: assert the helper function names this test relies
    on (`log`, `sentinel_mtime`, `run`) are present in
    `deploy/scheduler.sh`. A rename refactor would otherwise
    silently break the extraction above; this test makes the drift
    visible at test discovery time.
    """
    source = SCHEDULER_SH.read_text()
    for name in ("log", "sentinel_mtime", "run"):
        assert f"\n{name}() {{" in source or source.startswith(f"{name}() {{"), (
            f"`{name}()` is no longer defined in deploy/scheduler.sh; "
            f"the extraction logic in this test file needs updating "
            f"to match. If the function was renamed, update both the "
            f"extraction names and the test snippets."
        )
