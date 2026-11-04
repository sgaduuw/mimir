"""Keep local browser checks optional, but never let CI silently skip them."""

import importlib
import os

import pytest


def playwright_api():
    if os.environ.get("MIMIR_BROWSER_REQUIRED") == "1":
        return importlib.import_module("playwright.sync_api")
    return pytest.importorskip("playwright.sync_api")
