import shutil
import subprocess
from pathlib import Path

import pytest


def test_theme_controller():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for JavaScript controller checks")
    result = subprocess.run(
        [node, "--test", "tests/js/theme.test.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_theme_control_and_privacy_notice(client):
    from html.parser import HTMLParser

    class Tags(HTMLParser):
        def handle_starttag(self, tag, attrs):
            tags.append((tag, dict(attrs)))

    tags = []
    body = client.get("/alpha/today").get_data(as_text=True)
    Tags().feed(body)
    selector = next(attrs for tag, attrs in tags if tag == "select")
    assert selector["aria-label"] == "Color theme"
    assert [attrs["value"] for tag, attrs in tags if tag == "option"] == [
        "system",
        "light",
        "dark",
    ]
    script = next(
        attrs
        for tag, attrs in tags
        if tag == "script" and "js/theme.js" in attrs.get("src", "")
    )
    assert "defer" not in script and "async" not in script
    assert body.index("js/theme.js") < body.index("pico.amber.min.css")
    assert "mimir.theme" in client.get("/privacy").get_data(as_text=True)
