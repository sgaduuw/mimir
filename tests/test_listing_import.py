import os
import subprocess
import sys


def test_application_imports_in_a_clean_process():
    env = dict(
        os.environ,
        MIMIR_ENV_FILE="",
        SECRET_KEY="test-secret-key-not-for-real-1234567890abc",
        PYTHONDONTWRITEBYTECODE="1",
    )
    result = subprocess.run(
        [sys.executable, "-c", "from mimir import create_app; create_app()"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
