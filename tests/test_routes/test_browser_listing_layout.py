"""Optional real layout check, using the site's templates and stylesheets.

Run with the same Playwright setup as test_browser_navigation.py. Screenshots
are written to pytest's temporary directory, whose path is printed with -s.
CI sets MIMIR_BROWSER_REQUIRED=1 and uploads those screenshots on failure.
"""

from datetime import UTC, datetime
from threading import Thread
from types import SimpleNamespace

from flask import render_template
from werkzeug.serving import make_server

from mimir.lifecycle_status import LifecycleStatusInfo, _format_count_suffix
from tests._browser import playwright_api
from tests._dom import parse


def presentation_pages(client):
    """Also usable by a local snapshot exporter for manual browser checks."""
    when = datetime(2026, 11, 3, 21, 2, 3, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            id=n,
            article_id=n,
            inbox_name="alpha",
            subject="[PATCH] " + "An unusually long subject with several words " * 3,
            author="address-only-sender@kernel.org",
            date=when,
            last_activity=when,
            reply_count=count,
            role=role,
        )
        for n, count, role in (
            (111, 123456, "Reported-and-tested-by"),
            (112, 0, "Reviewed-by"),
        )
    ]
    common = dict(
        inbox_name="alpha",
        current_inbox="alpha",
        lifecycle_status_by_id={
            111: LifecycleStatusInfo(
                state_value="reviewed",
                pill_label="REVIEWED",
                count_suffix=_format_count_suffix(1234, 12),
            )
        },
    )
    cases = {
        "author": dict(sub="Sender", results=rows),
        "reviewer": dict(
            address="reviewer@kernel.org",
            entries=rows,
            total=2,
            role_counts={row.role: 1 for row in rows},
            reviewer_authors={row.id: row.author for row in rows},
        ),
        "search": dict(
            query="patch",
            matching_count=123457,
            min_len=2,
            max_len=200,
            results=[
                SimpleNamespace(
                    representative=row,
                    root_id=row.id,
                    destination_date=row.date,
                    authors=[row.author],
                    count=count,
                    positions=[],
                    version="v1",
                )
                for row, count in zip(rows, (123456, 1), strict=True)
            ],
        ),
        "month": dict(
            year=2026,
            month_label="November 2026",
            total_messages=71422,
            matching_threads=15243,
            thread_cap=100,
            selected_filter="all",
            threads=rows,
        ),
        "inbox": dict(active=rows),
    }
    with client.application.test_request_context("/alpha/"):
        return {
            name: render_template(f"{name}.html", **common, **context)
            for name, context in cases.items()
        }


def test_layout_samples_exercise_long_values_and_all_listing_shapes(client):
    """Keep the optional browser's inputs runnable when Playwright is absent."""
    pages = presentation_pages(client)
    assert set(pages) == {"author", "reviewer", "search", "month", "inbox"}
    for name, html in pages.items():
        page = parse(html)
        assert len(page.find_all("tr", cls="listing-row")) == 2, name
        assert page.find("span", cls="badge-count").text() == (
            "· 1,234 reviews, 12 by maintainers"
        )
    reviewer = parse(pages["reviewer"])
    assert reviewer.find_all("td", cls="listing-role")[0].text() == (
        "Reported-and-tested-by"
    )
    assert "71,422 messages across 15,243 threads" in parse(pages["month"]).text()


def test_listing_layout_at_desktop_and_phone_widths(client, tmp_path):
    playwright = playwright_api()
    pages = presentation_pages(client)
    app = client.application
    app.add_url_rule("/_layout/<name>", view_func=lambda name: pages[name])
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p, p.chromium.launch() as browser:
            page = browser.new_page()
            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 900})
                for theme in ("light", "dark"):
                    for name in pages:
                        page.goto(
                            f"http://127.0.0.1:{server.server_port}/_layout/{name}"
                        )
                        page.evaluate(
                            "theme => document.documentElement.dataset.theme = theme",
                            theme,
                        )
                        # The site relies on Pico; a failed CDN load is not a pass.
                        assert page.evaluate(
                            "getComputedStyle(document.documentElement)"
                            ".getPropertyValue('--pico-font-family')"
                        ).strip()
                        shot = tmp_path / f"{name}-{width}-{theme}.png"
                        page.screenshot(path=str(shot), full_page=True)
                        print(shot)
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        ), (name, width, theme)
                        errors = page.locator(".listing-table").evaluate(
                            """table => {
                              const errors = [];
                              for (const cell of table.querySelectorAll('th, td')) {
                                if (cell.scrollWidth > cell.clientWidth + 1)
                                  errors.push('clipped: ' + cell.textContent.trim());
                              }
                              for (const el of table.querySelectorAll(
                                'thead th, time, .listing-count, .listing-role .badge'
                              )) {
                                // Element ranges include the 44px link box as well
                                // as its centered text. Only glyph rects describe lines.
                                const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                                const tops = new Set();
                                let node;
                                while ((node = walker.nextNode())) {
                                  if (!node.textContent.trim()) continue;
                                  const r = document.createRange();
                                  r.selectNodeContents(node);
                                  for (const rect of r.getClientRects())
                                    if (rect.height > 0) tops.add(Math.round(rect.top));
                                }
                                if (tops.size > 1) errors.push('wrapped: ' + el.textContent.trim());
                              }
                              for (const heading of table.querySelectorAll('.listing-day th'))
                                if (getComputedStyle(heading).textAlign !== 'left')
                                  errors.push('day heading not left aligned');
                              return errors;
                            }"""
                        )
                        assert errors == [], (name, width, theme, errors)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def polish_pages(client):
    """Real template samples for inline marks, chip punctuation, headers and year layout."""
    when = datetime(2026, 11, 4, 12, tzinfo=UTC)
    row = SimpleNamespace(
        id=111,
        article_id=111,
        inbox_name="alpha",
        subject="memfd_pin_folios()",
        author="Ann Author <ann@example.org>",
        date=when,
        last_activity=when,
        reply_count=1234,
    )
    with client.application.test_request_context("/alpha/"):
        search = render_template(
            "search.html",
            inbox_name="alpha",
            current_inbox="alpha",
            query="folio",
            min_len=2,
            max_len=200,
            matching_count=1234,
            results=[
                SimpleNamespace(
                    representative=row,
                    root_id=row.id,
                    destination_date=when,
                    authors=[row.author],
                    count=1234,
                    positions=[],
                    version="v1",
                )
            ],
            lifecycle_status_by_id={
                111: LifecycleStatusInfo(
                    state_value="reviewed",
                    pill_label="REVIEWED",
                    count_suffix=_format_count_suffix(1234, 12),
                )
            },
        )
        year = render_template(
            "year.html",
            inbox_name="alpha",
            current_inbox="alpha",
            year=2026,
            volume=SimpleNamespace(
                total=596487, months=[(1, 596487)] + [(m, 0) for m in range(2, 13)]
            ),
            is_current_year=True,
            current_month=11,
            prev_year=2025,
            next_year=2027,
        )
    return {"search": search, "year": year}


def test_polish_samples_exercise_midword_mark_and_year_total(client):
    pages = polish_pages(client)
    client.application.add_url_rule(
        "/_polish/<name>", view_func=lambda name: pages[name]
    )
    search = parse(client.get("/_polish/search").get_data(as_text=True))
    subject = search.find("a", cls="listing-subject")
    assert subject.text() == "memfd_pin_folios()"
    assert subject.find("mark").text() == "folio"
    badge = search.find("span", cls="badge-lifecycle-reviewed")
    assert badge.text() == "REVIEWED · 1,234 reviews, 12 by maintainers"
    year = parse(client.get("/_polish/year").get_data(as_text=True))
    total = year.find("p", cls="year-total")
    assert total.text() == "596,487 messages so far this year"
    assert year.find("nav", cls="year-nav").find_all("small") == []


def test_inline_marks_chips_headers_and_year_alignment(client, tmp_path):
    playwright = playwright_api()
    pages = polish_pages(client)
    app = client.application
    app.add_url_rule("/_polish/<name>", view_func=lambda name: pages[name])
    server = make_server("127.0.0.1", 0, app, threaded=True)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with playwright.sync_playwright() as p, p.chromium.launch() as browser:
            page = browser.new_page()
            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 900})
                for theme in ("light", "dark"):
                    for name in pages:
                        page.goto(
                            f"http://127.0.0.1:{server.server_port}/_polish/{name}"
                        )
                        page.evaluate(
                            "theme => document.documentElement.dataset.theme=theme",
                            theme,
                        )
                        assert page.evaluate(
                            "getComputedStyle(document.documentElement).getPropertyValue('--pico-font-family')"
                        ).strip()
                        page.screenshot(
                            path=str(tmp_path / f"polish-{name}-{width}-{theme}.png"),
                            full_page=True,
                        )
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        ), (name, width, theme)
                        if name == "search":
                            subject = page.locator(".listing-subject")
                            assert subject.text_content() == "memfd_pin_folios()"
                            assert subject.locator("mark").evaluate(
                                "e => [getComputedStyle(e).paddingLeft,getComputedStyle(e).paddingRight]"
                            ) == ["0px", "0px"]
                            headers = page.locator(
                                ".listing-table thead th"
                            ).evaluate_all(
                                "cells => cells.map(e => [getComputedStyle(e).fontSize,getComputedStyle(e).color])"
                            )
                            assert len(headers) == 3 and all(
                                style == headers[0] for style in headers
                            ), headers
                            badge = " ".join(
                                page.locator(".badge-lifecycle-reviewed")
                                .text_content()
                                .split()
                            )
                            assert (
                                badge == "REVIEWED · 1,234 reviews, 12 by maintainers"
                            )
                            assert ":" not in badge
                        else:
                            positions = page.evaluate("""() => {
                              const selectors=['h1','.year-total','.year-rows','.year-nav'];
                              const boxes=selectors.map(s => document.querySelector(s).getBoundingClientRect());
                              return {left:boxes.map(b => b.left),width:boxes.slice(2).map(b => b.width),
                                afterHeading:document.querySelector('.year-total').previousElementSibling.tagName==='H1'};
                            }""")
                            assert positions["afterHeading"]
                            assert (
                                max(positions["left"]) - min(positions["left"]) < 1
                            ), positions
                            assert (
                                abs(positions["width"][0] - positions["width"][1]) < 1
                            ), positions
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
