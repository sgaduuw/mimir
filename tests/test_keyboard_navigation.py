"""Exercise the real keyboard controller with Node's native test runner."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_keyboard_navigation_controller():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for JavaScript controller checks")
    result = subprocess.run(
        [
            node,
            "--test",
            "tests/js/keyboard-nav.test.cjs",
            "tests/js/series-diff.test.cjs",
            "tests/js/htmx-navigation.test.cjs",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
