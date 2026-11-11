"""Keep local browser checks optional, but never let CI silently skip them."""

import importlib
import os
from urllib.parse import urlsplit

import pytest


def playwright_api():
    if os.environ.get("MIMIR_BROWSER_REQUIRED") == "1":
        return importlib.import_module("playwright.sync_api")
    return pytest.importorskip("playwright.sync_api")


def block_external_requests(page, base):
    """Prove real frontend assets work without a third-party connection."""
    origin = urlsplit(base)
    blocked = []

    def route_request(route):
        url = urlsplit(route.request.url)
        if (url.scheme, url.netloc) == (origin.scheme, origin.netloc):
            route.continue_()
        else:
            blocked.append(route.request.url)
            route.abort()

    page.route("**/*", route_request)
    return blocked
