import shutil
import subprocess
from pathlib import Path

import pytest


def test_list_tools_script():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for JavaScript controller checks")
    result = subprocess.run(
        [node, "--test", "tests/js/list-tools.test.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
