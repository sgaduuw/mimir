"""Optional real-browser regression for HTMX navigation.

Run: uv run --frozen --with playwright pytest -q tests/test_routes/test_browser_navigation.py
Install Chromium once: uv run --with playwright playwright install chromium
"""

from threading import Thread

import pytest
from werkzeug.serving import make_server

from mimir.config import settings
from tests.test_routes._helpers import build_thread


def test_htmx_navigation_and_progressive_links(client, tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    expect = playwright.expect
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    messages = list(build_thread(tmp_path, "alpha", shape="fan_out", size=12).values())
    root_id, root_path = messages[0]
    next_id, next_path = messages[1]
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with playwright.sync_playwright() as p, p.chromium.launch() as browser:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + root_path)
            assert page.evaluate("typeof htmx") == "object"
            page.locator('[data-fold-set="expanded"]').click()

            def expect_message(article_id, path):
                expect(page.locator("#msg")).to_have_attribute(
                    "data-article-id", str(article_id)
                )
                expect(page.locator(".thread-list li.is-active")).to_have_attribute(
                    "data-article-id", str(article_id)
                )
                expect(page).to_have_url(base + path)

            with page.expect_response(
                lambda r: (
                    r.url == base + next_path
                    and r.request.headers.get("hx-request") == "true"
                )
            ) as response:
                page.locator(f'.thread-list a[href="{next_path}"]').click()
            assert response.value.status == 200
            expect_message(next_id, next_path)
            page.go_back()
            expect_message(root_id, root_path)
            page.go_forward()
            expect_message(next_id, next_path)

            page.locator('[data-fold-set="closed"]').click()
            expect(page.locator("html")).to_have_attribute("data-thread-fold", "closed")
            page.locator('[data-fold-set="expanded"]').click()
            expect(page.locator("html")).to_have_attribute(
                "data-thread-fold", "expanded"
            )

            page.keyboard.press("k")
            expect_message(root_id, root_path)
            page.keyboard.press("j")
            expect_message(next_id, next_path)

            # A failed swap must preserve the currently readable message.
            page.evaluate(
                """() => document.body.addEventListener('htmx:responseError',
                () => { document.body.dataset.responseError = 'received'; },
                {once: true})"""
            )
            page.route(base + root_path, lambda route: route.fulfill(status=500))
            with page.expect_response(lambda r: r.status == 500):
                page.keyboard.press("k")
            expect(page.locator("body")).to_have_attribute(
                "data-response-error", "received"
            )
            expect_message(next_id, next_path)
            page.unroute(base + root_path)
            assert not errors, errors

            page.goto(base + "/alpha/")
            recent = page.locator("section").filter(
                has=page.get_by_role("heading", name="Recent messages", exact=True)
            )
            expect(recent.locator("li:not(.recent-more-trigger)")).to_have_count(10)
            recent.get_by_role("button", name="Load more", exact=True).click()
            expect(recent.locator("li")).to_have_count(15)
            expect(recent.locator(".recent-more-trigger")).to_have_count(0)

            thread_path = root_path + "/t"
            page.goto(base + thread_path)
            expect(page.locator(".thread-message")).to_have_count(5)
            page.locator(".thread-more a").click()
            expect(page.locator(".thread-message")).to_have_count(10)
            page.locator(".thread-more a").click()
            expect(page.locator(".thread-message")).to_have_count(12)
            expect(page.locator(".thread-more")).to_have_count(0)
            assert not errors, errors

            with browser.new_context(java_script_enabled=False) as context:
                plain = context.new_page()
                plain.goto(base + root_path)
                plain.locator(f'.thread-list a[href="{next_path}"]').click()
                expect(plain.locator("#msg")).to_have_attribute(
                    "data-article-id", str(next_id)
                )
                plain.goto(base + thread_path)
                next_page = plain.locator(".thread-more a").get_attribute("href")
                plain.locator(".thread-more a").click()
                expect(plain).to_have_url(base + next_page)
                expect(plain.locator(".thread-message")).to_have_count(5)
                expect(plain.locator(f"#m{root_id}")).to_have_count(0)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
