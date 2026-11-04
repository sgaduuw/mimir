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
            ": 1234 reviews, 12 by maintainers"
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
