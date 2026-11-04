import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("controller", ["thread-anchors", "thread-fold", "thread-tabs"])
def test_thread_controller_script(controller):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for JavaScript controller checks")
    result = subprocess.run(
        [node, "--test", f"tests/js/{controller}.test.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
