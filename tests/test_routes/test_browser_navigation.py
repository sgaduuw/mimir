"""Optional real-browser regression for HTMX navigation.

Run: uv run --frozen --with playwright pytest -q tests/test_routes/test_browser_navigation.py
Install Chromium once: uv run --with playwright playwright install chromium
"""

from threading import Thread

import pytest
from werkzeug.serving import make_server

from mimir.config import settings
from tests.test_routes._helpers import build_thread, seed_thread_shape


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
            partial = response.value
            assert partial.status == 200
            assert "<!doctype html>" not in partial.text().lower()
            assert "hx-request" in partial.headers["vary"].lower()
            assert partial.headers["etag"]
            expect_message(next_id, next_path)
            page.go_back()
            expect_message(root_id, root_path)
            page.go_forward()
            expect_message(next_id, next_path)

            # Force real cache misses in both directions, after checking cache hits.
            for navigate, article_id, path in [
                (page.go_back, root_id, root_path),
                (page.go_forward, next_id, next_path),
            ]:
                cached = page.evaluate(
                    "JSON.parse(sessionStorage.getItem('htmx-history-cache'))"
                )
                assert any(entry["url"] == path for entry in cached)
                page.evaluate("sessionStorage.removeItem('htmx-history-cache')")
                with page.expect_response(
                    lambda r: (
                        r.url == base + path
                        and r.request.headers.get("hx-history-restore-request")
                        == "true"
                    )
                ) as response:
                    navigate()
                restored = response.value
                assert restored.status == 200
                expect(page.locator("#msg")).to_have_attribute(
                    "data-article-id", str(article_id)
                )
                expect(page.locator("main > nav")).to_be_visible()
                expect_message(article_id, path)
                assert "hx-request" not in restored.request.headers
                assert "<!doctype html>" in restored.text().lower()
                assert "hx-request" in restored.headers["vary"].lower()
                if path == next_path:
                    assert restored.headers["etag"] != partial.headers["etag"]

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

            later_id, later_path = messages[5]
            flat_view = page.get_by_role("link", name="flat view", exact=True)
            for article_id, path, destination in [
                (later_id, later_path, root_path + "/t/2"),
                (root_id, root_path, root_path + "/t"),
                (later_id, later_path, root_path + "/t/2"),
            ]:
                page.locator(f'.thread-list a[href="{path}"]').click()
                expect_message(article_id, path)
                expect(flat_view).to_have_attribute("href", destination)
            page.get_by_role("link", name="flat view", exact=True).click()
            expect(page).to_have_url(base + root_path + "/t/2")
            expect(page.locator(f"#m{later_id}")).to_be_visible()

            page.goto(base + "/alpha/")
            recent = page.locator("section").filter(
                has=page.get_by_role("heading", name="Recent messages", exact=True)
            )
            expect(recent.locator("li:not(.recent-more-trigger)")).to_have_count(10)
            recent.get_by_role("button", name="Load more", exact=True).click()
            expect(recent.locator("li")).to_have_count(15)
            expect(recent.locator(".recent-more-trigger")).to_have_count(0)

            thread_path = root_path + "/t"
            page.goto(base + root_path)
            page.get_by_role("link", name="flat view", exact=True).click()
            expect(page).to_have_url(base + thread_path)
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
                plain.get_by_role("link", name="flat view", exact=True).click()
                expect(plain).to_have_url(base + thread_path)
                next_page = plain.locator(".thread-more a").get_attribute("href")
                plain.locator(".thread-more a").click()
                expect(plain).to_have_url(base + next_page)
                expect(plain.locator(".thread-message")).to_have_count(5)
                expect(plain.locator(f"#m{root_id}")).to_have_count(0)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_keyboard_j_stays_on_messages_in_a_windowed_thread(
    client, tmp_path, monkeypatch
):
    """A long thread is windowed (#798). Its gap and footer rows link to
    the thread view; j at the last message shown must not follow them."""
    playwright = pytest.importorskip("playwright.sync_api")
    expect = playwright.expect
    monkeypatch.setattr(settings, "thread_view_render_cap", 5)
    messages = list(build_thread(tmp_path, "alpha", shape="fan_out", size=30).values())
    root_path = messages[0][1]
    last_id, last_path = messages[10]  # the root's 10th reply, the last shown
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with playwright.sync_playwright() as p, p.chromium.launch() as browser:
            page = browser.new_page()
            page.goto(base + root_path)
            page.locator('[data-fold-set="expanded"]').click()
            # Precondition: the row after the last reply is a gap row.
            expect(page.locator(".thread-list li.thread-gap a")).to_have_count(1)
            page.locator(f'.thread-list a[href="{last_path}"]').click()
            expect(page.locator(".thread-list li.is-active")).to_have_attribute(
                "data-article-id", str(last_id)
            )

            page.keyboard.press("j")
            page.wait_for_timeout(500)

            expect(page).to_have_url(base + last_path)
            expect(page.locator("#msg")).to_have_attribute(
                "data-article-id", str(last_id)
            )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_an_in_page_jump_into_an_appended_page_names_that_page(
    client, tmp_path, monkeypatch
):
    """After "Next" appends page 2, a "replied to" link or an outline row to
    a message on it scrolls in place (#1035), and the address bar names
    page 2: setting only the hash kept page 1's path, so a copied address
    or a reload lost the message (3.39.0 release review)."""
    playwright = pytest.importorskip("playwright.sync_api")
    expect = playwright.expect
    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    messages = list(build_thread(tmp_path, "alpha", shape="chain", size=6).values())
    root_path = messages[0][1]
    page1, page2 = root_path + "/t", root_path + "/t/2"
    on_page2 = [messages[2][0], messages[3][0]]
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with playwright.sync_playwright() as p, p.chromium.launch() as browser:
            page = browser.new_page()
            page.goto(base + page1)
            # Precondition: page 1 does not hold the messages of page 2.
            expect(page.locator(f"#m{on_page2[0]}")).to_have_count(0)
            # A marker a full load would wipe.
            page.evaluate("window.stayed = true")
            page.locator(f'.thread-more a[href="{page2}"]').click()
            expect(page.locator(f"#m{on_page2[1]}")).to_be_attached()
            # A clicked permalink scrolls in place, while the address bar
            # still names page 1 (on page 2 already, the browser's own
            # fragment navigation would pass without the script).
            expect(page).to_have_url(base + page1)
            page.locator(f"#m{on_page2[1]} a.permalink").click()
            expect(page).to_have_url(f"{base}{page2}#m{on_page2[1]}")
            assert page.evaluate("window.stayed") is True
            page.goto(base + page1)
            page.evaluate("window.stayed = true")
            page.locator(f'.thread-more a[href="{page2}"]').click()
            expect(page.locator(f"#m{on_page2[1]}")).to_be_attached()
            # The chain's 4th message answers the 3rd, both on page 2.
            page.locator(f"#m{on_page2[1]} .replied-to a").click()
            expect(page).to_have_url(f"{base}{page2}#m{on_page2[0]}")
            expect(page.locator(f"#m{on_page2[0]}")).to_be_in_viewport()
            page.evaluate("document.querySelector('.thread-outline').open = true")
            page.locator(f'.thread-outline a[href="{page2}#m{on_page2[1]}"]').click()
            expect(page).to_have_url(f"{base}{page2}#m{on_page2[1]}")
            assert page.evaluate("window.stayed") is True
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_an_in_page_jump_opens_the_tab_that_holds_the_message(
    client, tmp_path, monkeypatch
):
    """On the Patches tab, an outline row to a reply on an appended page
    opens Discussion: the jump pushes the URL, which fires no hashchange,
    so thread-anchors.js sends one for thread-tabs.js. Written by the
    3.39.0 fix review, red with that dispatch removed."""
    playwright = pytest.importorskip("playwright.sync_api")
    expect = playwright.expect
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    monkeypatch.setattr(settings, "thread_view_render_cap", 3)
    edges = [
        ("cov@x", None),
        ("p1@x", "cov@x"),
        ("p2@x", "cov@x"),
        ("p3@x", "cov@x"),
        ("rev@x", "p1@x"),
        ("rev2@x", "rev@x"),
    ]
    seeded = seed_thread_shape(tmp_path, "alpha", edges)
    with SessionLocal() as s:
        for pos, mid in enumerate(["cov@x", "p1@x", "p2@x", "p3@x"]):
            a = s.get(Article, seeded[mid][0])
            a.subject = f"[PATCH v2 {pos}/3] foo: part {pos}"
            a.patch_series_key = "t" * 40
            a.patch_series_version = "v2"
            a.patch_series_position = pos
        s.commit()
    page1 = seeded["cov@x"][1] + "/t"
    reply = seeded["rev2@x"][0]
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with playwright.sync_playwright() as p, p.chromium.launch() as browser:
            page = browser.new_page()
            page.goto(base + page1)
            expect(page.locator(".thread-tabs")).to_be_visible()
            more = page.locator(".thread-more a")
            page2 = more.get_attribute("href")
            more.click()
            expect(page.locator(f"#m{reply}")).to_be_attached()
            page.locator('[role="tab"][data-tab="patches"]').click()
            # Precondition: the reply is hidden, and its row names page 2.
            expect(page.locator(f"#m{reply}")).to_be_hidden()
            page.evaluate("document.querySelector('.thread-outline').open = true")
            row = page.locator(f'.thread-outline a[href="{page2}#m{reply}"]')
            expect(row).to_have_count(1)
            row.click()
            expect(page.locator("#thread-messages")).to_have_attribute(
                "data-tab", "discussion"
            )
            expect(page.locator(f"#m{reply}")).to_be_in_viewport()
            expect(page).to_have_url(f"{base}{page2}#m{reply}")
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
