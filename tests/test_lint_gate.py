"""The lint gate is a contract, so assert it here rather than only in CI.

`.github/workflows/ci.yml`'s `lint` job runs exactly
`uv run ruff check mimir/ tests/` and `uv run ruff format --check mimir/
tests/`. `main` requires that job (CONTEXT.md "CI flow"), and the release
flow's Step 4 repeats both commands as a release gate.

Both tests below shell out to the ruff pinned in `uv.lock`, which is the
version CI resolves via `uv sync --frozen`. Running the real binary rather
than reimplementing any part of the rule set is the point: the thing worth
pinning is "the command CI runs exits 0", and any reimplementation would
answer a different question.
"""

import shutil
import subprocess
import sys

import pytest

PATHS = ["mimir/", "tests/"]


def _ruff(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the project's ruff the way CI does, from the repo root.

    `python -m ruff` rather than a bare `ruff` so the resolved
    interpreter's environment is the one under test, matching `uv run`.
    """
    if shutil.which("ruff") is None and not _module_available():
        pytest.skip("ruff is not installed in this environment")
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args],
        capture_output=True,
        text=True,
        timeout=300,
        # A non-zero exit IS the signal both tests read; raising here
        # would lose the finding list that makes the failure actionable.
        check=False,
    )


def _module_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("ruff") is not None


def test_ruff_check_exits_zero():
    """`ruff check mimir/ tests/` must pass.

    This is `ci.yml`'s `lint` step verbatim. It is red on
    `chore/explicit-ruff-ruleset` because that branch raises the ruff pin
    from `>=0.15.21,<0.16` to `>=0.16.5,<0.17` and enables an explicit
    `select` list, then leaves the findings that newly-enabled set
    produces unfixed. develop's `lint` job is green only because develop
    still resolves ruff 0.15.21, whose narrow default set does not look
    for them.

    A staged rollout is fine, but it has to keep the gate green while it
    is staged: put the not-yet-addressed rule codes in
    `[tool.ruff.lint] ignore` with a comment naming the follow-up, so
    each later removal from `ignore` is its own reviewable step. Leaving
    `ruff check` red instead blocks the next `release/X.Y.Z -> main` PR,
    where `lint` is a required check and cannot be waved through.
    """
    proc = _ruff("check", *PATHS)
    counts = proc.stdout.strip().splitlines()[-1:] or [""]
    assert proc.returncode == 0, (
        f"ruff check exited {proc.returncode}; CI's lint job runs this exact "
        f"command and will fail.\nLast line: {counts[0]}\n"
        "Per-rule breakdown: uv run ruff check --statistics mimir/ tests/"
    )


def test_ruff_autofix_left_no_e402_behind():
    """No E402 anywhere, because this branch's own autofix created the
    only two in the tree.

    Narrower than the test above on purpose: these two findings are not
    pre-existing debt that the wider rule set merely started looking for,
    they were *introduced* by commit 3a3c529's safe-autofix pass and are
    the only findings in the branch with that provenance.

    `tests/conftest.py` and `tests/test_ingest/test_orchestrate.py` each
    carry a deliberate mid-file `import pytest  # noqa: E402`. Ruff's
    isort treats that suppressed import as a second import block, so the
    UP017 rewrite's new `from datetime import UTC` was inserted *into*
    that block, below `os.environ[...]` assignments in one file and below
    three test functions in the other, and without the `# noqa: E402`
    its neighbour has.

    Checked apples-to-apples: running this branch's config against the
    develop tree reports `All checks passed!` for E402, so develop has
    none and HEAD has two. Fix is either the same `# noqa: E402` comment
    the adjacent line already carries, or hoisting the import to the top
    of the file where it belongs.
    """
    proc = _ruff("check", "--select", "E402", "--output-format", "concise", *PATHS)
    hits = [ln for ln in proc.stdout.splitlines() if "E402" in ln]
    assert not hits, "ruff's autofix introduced E402 violations:\n  " + "\n  ".join(
        hits
    )
