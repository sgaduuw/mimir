"""A missing optional browser dependency cannot silently skip the CI gate."""

import sys

import pytest

from tests._browser import playwright_api


@pytest.mark.parametrize("required", [False, True])
def test_missing_playwright_fails_only_when_browser_gate_is_required(
    monkeypatch, required
):
    monkeypatch.setenv("MIMIR_BROWSER_REQUIRED", "1" if required else "0")
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    expected = ModuleNotFoundError if required else pytest.skip.Exception
    with pytest.raises(expected):
        playwright_api()
