"""Optional real layout check, using the site's templates and stylesheets.

Run with the same Playwright setup as test_browser_navigation.py. Screenshots
are written to pytest's temporary directory, whose path is printed with -s.
CI sets MIMIR_BROWSER_REQUIRED=1 and uploads those screenshots on failure.
"""

import re
from datetime import UTC, datetime
from threading import Thread
from types import SimpleNamespace

from flask import render_template
from werkzeug.serving import make_server

from mimir.lifecycle_status import LifecycleStatusInfo, _format_count_suffix
from tests._browser import block_external_requests, playwright_api
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
            blocked = block_external_requests(
                page, f"http://127.0.0.1:{server.server_port}"
            )
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
                        # The real vendored Pico stylesheet must have loaded.
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
            assert blocked == [], blocked
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


def release_pages(client):
    """A release history page with subsystem sections and the "smaller
    subsystems" row (#1136), rendered from the real template."""
    from mimir.merge_window import MergeWindowSection
    from mimir.release_history import ReleaseSubsystems

    def section(name, patches):
        return MergeWindowSection(
            name=name, anchor=name.lower().replace(" ", "-"), patches=patches, rows=[]
        )

    sections = [
        section("NETWORKING DRIVERS (INCLUDING A VERY LONG SUBSYSTEM NAME)", 7000),
        section("KERNEL VIRTUAL MACHINE FOR X86 (KVM/x86)", 12345),
        section("DRM DRIVERS", 40),
    ]
    smaller = [section("TINY ONE", 20), section("(no subsystem)", 1)]
    when = datetime(2026, 11, 3, 21, 2, 3, tzinfo=UTC)
    article = SimpleNamespace(
        subject="[PATCH] a subject", author="Ann Author <a@x>", date=when
    )
    with client.application.test_request_context("/merge-window/history/v7.3"):
        html = render_template(
            "release_history_detail.html",
            release=SimpleNamespace(tag="v7.3", previous_tag="v7.2"),
            facts=SimpleNamespace(
                release_date=when, date_source="tagger", commit_count=14126
            ),
            links=SimpleNamespace(pair_count=14126),
            rows=[(article, "/alpha/2026/11/1")],
            total=14040,
            subsystems=ReleaseSubsystems(
                sections=sections,
                smaller=smaller,
                smaller_patches=21,
                references=19425,
            ),
            handful=10,
            section_min=25,
            page=1,
            previous=None,
            following=None,
            current_inbox=None,
            canonical_url="http://localhost/merge-window/history/v7.3",
        )
    return {"release": html}


def test_release_sample_has_sections_and_the_smaller_row(client):
    """Keep the browser test's input runnable without Playwright."""
    page = parse(release_pages(client)["release"])
    listing = page.find("ul", id="mw-subsystems")
    flat = listing.find_all("div", cls="mw-flat")
    assert len(flat) == 5
    assert listing.find_all("details")[0].attrs["id"] == "smaller-subsystems"
    assert "of this release's recorded references" in page.text()


def test_release_subsystem_rows_at_desktop_and_phone_widths(client, tmp_path):
    playwright = playwright_api()
    pages = release_pages(client)
    app = client.application
    app.add_url_rule("/_release/<name>", view_func=lambda name: pages[name])
    server = make_server("127.0.0.1", 0, app, threaded=True)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with playwright.sync_playwright() as p, p.chromium.launch() as browser:
            page = browser.new_page()
            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 900})
                for theme in ("light", "dark"):
                    page.goto(f"http://127.0.0.1:{server.server_port}/_release/release")
                    page.evaluate(
                        "theme => document.documentElement.dataset.theme = theme",
                        theme,
                    )
                    assert page.evaluate(
                        "getComputedStyle(document.documentElement)"
                        ".getPropertyValue('--pico-font-family')"
                    ).strip()
                    # The smaller row is a closed disclosure; open it so its
                    # nested rows are measured too.
                    page.evaluate(
                        "document.getElementById('smaller-subsystems').open = true"
                    )
                    page.screenshot(
                        path=str(tmp_path / f"release-{width}-{theme}.png"),
                        full_page=True,
                    )
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    ), (width, theme)
                    errors = page.locator("#mw-subsystems").evaluate(
                        """list => {
                          const errors = [];
                          for (const row of list.querySelectorAll('.mw-flat, summary')) {
                            const box = row.getBoundingClientRect();
                            for (const el of row.querySelectorAll('.mw-name, .count, .bar-meter')) {
                              const r = el.getBoundingClientRect();
                              if (r.right > box.right + 1 || r.left < box.left - 1)
                                errors.push('outside its row: ' + el.getAttribute('class'));
                            }
                            // The count stays on one line, whatever its width.
                            const count = row.querySelector('.count');
                            if (count.getClientRects().length !== 1
                                || count.getBoundingClientRect().height > 40)
                              errors.push('count wraps: ' + count.textContent);
                            const bar = row.querySelector('.bar-meter');
                            if (bar && bar.getBoundingClientRect().width < 10)
                              errors.push('bar collapsed: ' + row.textContent.trim());
                            if (!bar) continue;  // the smaller-subsystems summary has none
                            // One grid for both kinds of row: on a wide screen
                            // name, bar and count share a line, on a phone the
                            // bar drops under the name.
                            const name = row.querySelector('.mw-name').getBoundingClientRect();
                            const bar2 = bar.getBoundingClientRect();
                            const sameLine = Math.abs(
                              (bar2.top + bar2.bottom) / 2 - (name.top + name.bottom) / 2
                            ) < 20;
                            if (sameLine !== (innerWidth >= 768))
                              errors.push('bar on the wrong line: ' + row.textContent.trim());
                          }
                          return errors;
                        }"""
                    )
                    assert errors == [], (width, theme, errors)
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def release_pull_pages(client):
    """A closed release's page with every pull-request state (#1135),
    long subjects and a card past four rows, rendered from the real
    template with its coverage note."""
    from mimir.release_history import ReleasePull, ReleasePulls, ReleasePullSummary

    when = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
    long = "[GIT PULL] " + "drivers and a very long pull request subject " * 3
    states = ["merged"] * 6 + ["not_merged"] * 6 + ["unknown", "rejected"]
    rows = [
        ReleasePull(
            subject=long if n % 2 else f"[GIT PULL] pull {n}",
            url=f"/alpha/2026/09/{n}",
            author="Mae Maintainer With A Long Name <mae@example.com>",
            date=when,
            state=state,
        )
        for n, state in enumerate(states)
    ]
    with client.application.test_request_context("/merge-window/history/v7.3"):
        html = render_template(
            "release_history_detail.html",
            release=SimpleNamespace(tag="v7.3", previous_tag="v7.2"),
            facts=SimpleNamespace(
                release_date=when, date_source="tagger", commit_count=20260
            ),
            links=SimpleNamespace(pair_count=14126),
            rows=[],
            total=0,
            subsystems=None,
            pulls=ReleasePulls(
                ReleasePullSummary(count=14, unclear=1, tips=13, bot_replies=0), rows
            ),
            lead_in_days=14,
            unclear_note_share=0.10,
            handful=10,
            section_min=25,
            page=1,
            previous=None,
            following=None,
            current_inbox=None,
            canonical_url="http://localhost/merge-window/history/v7.3",
        )
    return {"pulls": html}


def test_release_pull_sample_has_all_four_states(client):
    """Keep the browser test's input runnable without Playwright."""
    page = parse(release_pull_pages(client)["pulls"])
    cards = page.find_all("section", cls="pull-card")
    assert [c.attrs["id"] for c in cards] == [
        "pulls-merged",
        "pulls-not_merged",
        "pulls-unknown",
        "pulls-rejected",
    ]
    assert cards[0].find_all("details") != []
    assert page.find_all("p", id="pulls-how-counted") != []


def test_release_pull_cards_at_desktop_and_phone_widths(client, tmp_path):
    playwright = playwright_api()
    pages = release_pull_pages(client)
    app = client.application
    app.add_url_rule("/_pulls/<name>", view_func=lambda name: pages[name])
    server = make_server("127.0.0.1", 0, app, threaded=True)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with playwright.sync_playwright() as p, p.chromium.launch() as browser:
            page = browser.new_page()
            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 900})
                for theme in ("light", "dark"):
                    page.goto(f"http://127.0.0.1:{server.server_port}/_pulls/pulls")
                    page.evaluate(
                        "theme => document.documentElement.dataset.theme = theme",
                        theme,
                    )
                    page.evaluate(
                        "document.querySelectorAll('.pull-card details')"
                        ".forEach(d => d.open = true)"
                    )
                    page.screenshot(
                        path=str(tmp_path / f"pulls-{width}-{theme}.png"),
                        full_page=True,
                    )
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    ), (width, theme)
                    layout = page.locator(".pull-cards").evaluate(
                        """grid => {
                          const errors = [];
                          const box = grid.getBoundingClientRect();
                          const cards = [...grid.querySelectorAll('.pull-card')];
                          for (const card of cards) {
                            const r = card.getBoundingClientRect();
                            if (r.left < box.left - 1 || r.right > box.right + 1)
                              errors.push('card outside the grid: ' + card.id);
                            for (const el of card.querySelectorAll('h3, .count, a, small, summary')) {
                              const e = el.getBoundingClientRect();
                              if (e.right > r.right + 1 || e.left < r.left - 1)
                                errors.push('outside its card: ' + card.id + ' ' + el.tagName);
                            }
                            const label = card.querySelector('h3');
                            if (getComputedStyle(label).color === getComputedStyle(card).backgroundColor)
                              errors.push('label invisible: ' + card.id);
                          }
                          const rows = new Set(cards.map(c => Math.round(c.getBoundingClientRect().top)));
                          return {errors, cards: cards.length, rows: rows.size};
                        }"""
                    )
                    assert layout["errors"] == [], (width, theme, layout)
                    assert layout["cards"] == 4
                    # The not-merged card is muted (#1139): its own surface,
                    # distinct from the other three, with readable text.
                    look = page.locator(".pull-cards").evaluate(
                        """grid => {
                          // Pico fades link colours; read the settled ones.
                          document.getAnimations().forEach(a => a.finish());
                          const lum = c => {
                            const v = c.match(/[\\d.]+/g).slice(0, 3).map(Number)
                              .map(x => { x /= 255;
                                return x <= .03928 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4; });
                            return .2126 * v[0] + .7152 * v[1] + .0722 * v[2];
                          };
                          const ratio = (a, b) => {
                            const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
                            return (hi + .05) / (lo + .05);
                          };
                          const out = {};
                          for (const card of grid.querySelectorAll('.pull-card')) {
                            const cs = getComputedStyle(card);
                            const bg = cs.backgroundColor;
                            out[card.id] = {
                              surface: [bg, cs.borderTopColor, cs.borderTopStyle].join(' '),
                              count: ratio(getComputedStyle(card.querySelector('.count')).color, bg),
                              label: ratio(getComputedStyle(card.querySelector('h3')).color, bg),
                              summary: card.querySelector('summary') && ratio(
                                getComputedStyle(card.querySelector('summary')).color, bg),
                              link: ratio(getComputedStyle(card.querySelector('a')).color, bg),
                              muted: card.classList.contains('pull-card--not-merged'),
                            };
                          }
                          return out;
                        }"""
                    )
                    assert [k for k, v in look.items() if v["muted"]] == [
                        "pulls-not_merged"
                    ], look
                    others = {
                        v["surface"] for k, v in look.items() if k != "pulls-not_merged"
                    }
                    assert look["pulls-not_merged"]["surface"] not in others, look
                    assert look["pulls-not_merged"]["count"] >= 4.5, look
                    assert look["pulls-not_merged"]["link"] >= 4.5, look
                    # The uppercase label and the "All N" summary are small text too.
                    assert look["pulls-not_merged"]["label"] >= 4.5, look
                    assert look["pulls-not_merged"]["summary"] >= 4.5, look
                    # Side by side on a wide screen, one per row on a phone.
                    assert (layout["rows"] < 4) is (width == 1280), (width, layout)
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_maintainer_activity_layout_and_keyboard_profile_links(
    client, session, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from mimir.maintainer_directory import maintainer_path
    from mimir.web.routes import dashboards
    from tests.test_routes.test_front_page import _seed_activity_profiles

    playwright = playwright_api()
    expect = playwright.expect
    # The aggregate prepares display names; the database keeps its blank-name case.
    rows = [
        {**row, "name": row["name"] or row["address"]}
        for row in _seed_activity_profiles(session)
    ]
    monkeypatch.setattr(dashboards, "most_active_maintainers", lambda *a, **kw: rows)
    # A subsystem section above, so the heading gap below it is measurable.
    monkeypatch.setattr(
        dashboards,
        "most_active_subsystems_global",
        lambda *a, **kw: [
            SimpleNamespace(
                name="ACTIVITY PROFILES", inbox_name="alpha", message_count=10
            )
        ],
    )
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with playwright.sync_playwright() as p, p.chromium.launch() as browser:
            for width in (390, 1280):
                for theme in ("light", "dark"):
                    page = browser.new_page(
                        viewport={"width": width, "height": 800}, color_scheme=theme
                    )
                    blocked = block_external_requests(page, base)
                    page.goto(base + "/")
                    expect(page.locator("#maintainer-activity")).to_have_text(
                        "Maintainer activity this week"
                    )
                    section = page.locator("ul.front-maintainers")
                    expect(section.locator(".count")).to_have_text(["1,234", "56"])
                    expect(section.locator(".front-row-meta")).to_have_text(
                        ["0 review trailers", "12 review trailers"]
                    )
                    expect(section.locator(".bar-meter")).to_have_count(2)
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    )
                    assert section.evaluate(
                        "ul => [...ul.querySelectorAll('li > *')].every(cell => cell.scrollWidth <= cell.clientWidth + 1)"
                    )
                    # Pico spaces a heading only after a sibling list; the heading
                    # must not sit flush against the subsystem rows above it.
                    assert (
                        page.evaluate(
                            "() => document.querySelector('#maintainer-activity').getBoundingClientRect().top - document.querySelector('.front-subsystems:not(.front-maintainers)').getBoundingClientRect().bottom"
                        )
                        >= 16
                    )
                    screenshot = tmp_path / f"maintainer-activity-{width}-{theme}.png"
                    section.screenshot(path=str(screenshot))
                    print(screenshot)
                    links = section.get_by_role("link")
                    expect(links).to_have_count(2)
                    expect(links).to_have_text([rows[0]["name"], rows[1]["address"]])
                    links.nth(0).focus()
                    page.keyboard.press("Tab")
                    expect(links.nth(1)).to_be_focused()
                    page.keyboard.press("Shift+Tab")
                    expect(links.nth(0)).to_be_focused()
                    page.keyboard.press("Enter")
                    expect(page).to_have_url(base + maintainer_path(rows[0]["address"]))
                    expect(page.get_by_role("heading", level=1)).to_contain_text(
                        rows[0]["name"]
                    )
                    assert blocked == [], blocked
                    page.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_directory_filter_sort_and_layout(client, session, tmp_path, monkeypatch):
    from mimir.maintainer_directory import maintainer_path
    from mimir.models import Subsystem, SubsystemMaintainer
    from mimir.subsystems import subsystem_path
    from mimir.web.routes import directory_pages

    playwright = playwright_api()
    expect = playwright.expect
    names = [
        "ALPHA DRIVER WITH A NAME THAT SHOULD TRUNCATE ON A PHONE",
        "ANOTHER DRIVER",
        "A QUIET DRIVER",
    ]
    subs = []
    for name in names:
        sub = Subsystem(name=name, status="Maintained")
        session.add(sub)
        subs.append(sub)
    session.flush()
    maintainers = []
    for n, name in enumerate(
        ["Alice Kroah Long Display Name For A Maintainer", "Anne", "Arthur"]
    ):
        address = name.split()[0].lower() + "@kernel.org"
        session.add(
            SubsystemMaintainer(
                subsystem_id=subs[n].id, role="M", name=name, address=address
            )
        )
        maintainers.append(
            {
                "name": name,
                "address": address,
                "path": maintainer_path(address),
                "subsystem_count": 1,
                "review_count": [2, 0, 12][n],
            }
        )
    session.commit()
    assert {name[0] for name in names} == {"A"}
    assert {row["name"][0] for row in maintainers} == {"A"}
    subsystem_rows = [
        {
            "name": name,
            "inbox_name": "alpha" if n < 2 else None,
            "path": subsystem_path("alpha", name) if n < 2 else None,
            "message_count": [2, 30, 0][n],
        }
        for n, name in enumerate(names)
    ]
    monkeypatch.setattr(
        directory_pages,
        "subsystem_directory_rows",
        lambda session: (subsystem_rows, True),
    )
    monkeypatch.setattr(
        directory_pages,
        "maintainer_directory_rows",
        lambda session: (maintainers, True),
    )
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def check_strip(page):
        strip = page.locator(".directory-letters")
        expect(strip.locator(":scope > *")).to_have_count(27)
        expect(strip.get_by_role("link")).to_have_count(1)
        expect(strip.locator('span[aria-disabled="true"]')).to_have_count(26)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        boxes = strip.evaluate("""nav => {
          const muted = document.createElement('span');
          muted.style.color = 'var(--pico-muted-color)';
          nav.append(muted);
          const expectedColor = getComputedStyle(muted).color;
          muted.remove();
          const origin = nav.getBoundingClientRect();
          return Array.from(nav.children, el => {
            const box = el.getBoundingClientRect();
            return {x:box.x-origin.x, y:box.y-origin.y, width:box.width, height:box.height,
              muted:el.tagName !== 'SPAN' || getComputedStyle(el).color === expectedColor};
          });
        }""")
        assert all(b["width"] >= 44 and b["height"] >= 44 and b["muted"] for b in boxes)
        assert max(b["width"] for b in boxes) - min(b["width"] for b in boxes) < 1
        assert max(b["height"] for b in boxes) - min(b["height"] for b in boxes) < 1
        rows = {}
        for box in boxes:
            rows.setdefault(round(box["y"]), []).append(round(box["x"]))
        first = next(iter(rows.values()))
        assert all(row == first[: len(row)] for row in rows.values()), rows
        return [
            (round(b["x"]), round(b["y"]), round(b["width"]), round(b["height"]))
            for b in boxes
        ]

    try:
        with playwright.sync_playwright() as p, p.chromium.launch() as browser:
            for kind, query, active_name in (
                ("subsystems", "another", "another driver"),
                ("maintainers", "anne@kernel.org", "Arthur"),
            ):
                for width in (390, 1280):
                    for theme in ("light", "dark"):
                        page = browser.new_page(
                            viewport={"width": width, "height": 900}, color_scheme=theme
                        )
                        blocked = block_external_requests(page, base)
                        page.goto(base + f"/{kind}/")
                        landing_boxes = check_strip(page)
                        root_rows = page.locator(".directory-table tbody tr")
                        expect(root_rows).to_have_count(2)
                        expect(page.locator("#directory-active")).to_have_text(
                            f"Most active {kind} in the last seven days"
                        )
                        expect(
                            page.get_by_role("button", name="Most active", exact=True)
                        ).to_have_attribute("aria-pressed", "true")
                        assert (
                            root_rows.first.locator(".directory-name").inner_text()
                            == active_name
                        )
                        page.get_by_role("button", name="A to Z", exact=True).click()
                        orders = root_rows.evaluate_all(
                            "rows => rows.map(r => Number(r.dataset.order))"
                        )
                        assert orders == sorted(orders)
                        page.get_by_role(
                            "button", name="Most active", exact=True
                        ).click()
                        assert (
                            root_rows.first.locator(".directory-name").inner_text()
                            == active_name
                        )
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        )
                        shot = tmp_path / f"directory-root-{kind}-{width}-{theme}.png"
                        page.screenshot(path=str(shot), full_page=True)
                        print(shot)
                        search = page.get_by_role(
                            "searchbox", name="Search all " + kind
                        )
                        search.fill("truncate" if kind == "subsystems" else "Kroah")
                        page.get_by_role("button", name="Search", exact=True).click()
                        expect(page.locator(".directory-table tbody tr")).to_have_count(
                            1
                        )
                        assert (
                            page.locator('link[rel="canonical"]')
                            .get_attribute("href")
                            .endswith(f"/{kind}/")
                        )
                        expect(page.locator('meta[name="robots"]')).to_have_attribute(
                            "content", re.compile(".*noindex.*")
                        )
                        page.goto(base + f"/{kind}/")
                        page.get_by_role("link", name="A", exact=True).click()
                        expect(page).to_have_url(base + f"/{kind}/a/")
                        assert check_strip(page) == landing_boxes
                        expect(
                            page.locator('.directory-letters a[aria-current="page"]')
                        ).to_have_text("A")
                        table = page.locator(".directory-table")
                        rows = table.locator("tbody tr")
                        expect(rows).to_have_count(3)
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        )
                        assert table.locator("td.listing-count").evaluate_all(
                            "cells => cells.every(c => c.scrollWidth <= c.clientWidth + 1)"
                        )
                        assert table.locator(".listing-subject").evaluate_all(
                            "items => items.every(el => getComputedStyle(el).whiteSpace === 'nowrap')"
                        )
                        subjects = table.locator(".listing-subject")
                        for subject in subjects.all():
                            expect(subject).to_have_attribute(
                                "title", subject.inner_text()
                            )
                        if width == 390:
                            assert (
                                table.locator("td.directory-name").first.evaluate(
                                    "cell => cell.getBoundingClientRect().width / cell.parentElement.getBoundingClientRect().width"
                                )
                                >= 0.55
                            )
                            expect(table.locator(".bar-meter").first).to_be_hidden()
                            assert table.locator(".directory-name a").first.evaluate(
                                "el => getComputedStyle(el).textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth"
                            )
                        shot = tmp_path / f"directory-{kind}-{width}-{theme}.png"
                        page.screenshot(path=str(shot), full_page=True)
                        print(shot)
                        page.get_by_role("searchbox", name="Find a " + kind[:-1]).fill(
                            query
                        )
                        expect(table.locator("tbody tr:visible")).to_have_count(1)
                        page.get_by_role("searchbox", name="Find a " + kind[:-1]).fill(
                            ""
                        )
                        page.get_by_role(
                            "button", name="Most active", exact=True
                        ).click()
                        expect(rows.first.locator(".listing-subject")).to_have_text(
                            active_name
                        )
                        expect(
                            page.get_by_role("button", name="Most active", exact=True)
                        ).to_have_attribute("aria-pressed", "true")
                        page.get_by_role("button", name="A to Z", exact=True).click()
                        link = table.locator("tbody a").first
                        link.focus()
                        expect(link).to_be_focused()
                        href = link.get_attribute("href")
                        page.keyboard.press("Enter")
                        expect(page).to_have_url(base + href)
                        expect(page.get_by_role("heading", level=1)).to_be_visible()
                        assert blocked == []
                        page.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_profile_recent_messages_layout_and_links(
    client, session, frozen_clock, tmp_path
):
    from xml.etree import ElementTree as ET

    from tests.test_routes.test_maintainer_feeds import (
        ADDRESS,
        NS,
        PATH,
        _maintainer,
        _message,
    )

    playwright = playwright_api()
    expect = playwright.expect
    _maintainer(session)
    subject = "A long patch subject " * 16
    _message(session, subject, ADDRESS, inboxes=("beta",))
    feed = client.get(PATH)
    assert feed.status_code == 200
    entry = ET.fromstring(feed.data).find("a:entry", NS)
    destination = (
        entry.find("a:link", NS).attrib["href"].removeprefix("http://localhost")
    )
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with playwright.sync_playwright() as p, p.chromium.launch() as browser:
            for width in (390, 1280):
                for theme in ("light", "dark"):
                    page = browser.new_page(
                        viewport={"width": width, "height": 900}, color_scheme=theme
                    )
                    block_external_requests(page, base)
                    page.goto(base + "/maintainers/" + ADDRESS)
                    section = page.locator("#recent-messages")
                    expect(section).to_be_visible()
                    expect(section.locator("a")).to_have_text(subject.strip())
                    expect(section.locator("a")).to_have_attribute("href", destination)
                    expect(section.locator("time")).to_be_visible()
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    )
                    screenshot = tmp_path / f"profile-recent-{width}-{theme}.png"
                    page.screenshot(path=str(screenshot), full_page=True)
                    print(screenshot)
                    page.close()
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_merge_window_rows_load_in_place_and_without_javascript(
    client, tmp_path, monkeypatch
):
    """#1259: a larger subsystem's "All N postings" link and a smaller
    subsystem's disclosure fetch the rows from the subsystem page with HTMX
    and swap them in place; without JavaScript the same links navigate."""
    import mimir.merge_window as mwmod
    import mimir.web.routes.merge_window as route
    from mimir.extensions import SessionLocal
    from tests.test_merge_window import MERGE_WINDOW_PATH, _seed_postings

    playwright = playwright_api()
    expect = playwright.expect
    monkeypatch.setattr(mwmod, "SECTION_MIN_PATCHES", 2)
    monkeypatch.setattr(route, "SECTION_ROW_CAP", 2)
    with SessionLocal() as s:
        _seed_postings(s, {"BIG": 4, "TINY": 1})
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    main = base + MERGE_WINDOW_PATH
    try:
        with playwright.sync_playwright() as p, p.chromium.launch() as browser:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            blocked = block_external_requests(page, base)
            page.goto(main)
            big = page.locator("details#big")
            big_rows = big.locator(".mw-rows > ul > li")
            expect(big_rows).to_have_count(2)
            more = big.get_by_role("link", name="All 4 postings in big")
            more.click()
            expect(big_rows).to_have_count(4)
            expect(big.locator(".mw-more")).to_have_count(0)
            expect(page).to_have_url(main)
            # A smaller subsystem: open the group, then the subsystem.
            page.locator("details#smaller-subsystems > summary").click()
            tiny = page.locator("details#tiny")
            expect(tiny.locator(".mw-rows a")).to_have_text(["1 posting in tiny"])
            tiny.locator("summary").click()
            expect(tiny.locator(".mw-rows > ul > li a")).to_have_text(
                ["[PATCH] tiny0@x"]
            )
            expect(page).to_have_url(main)
            screenshot = tmp_path / "merge-window-htmx.png"
            page.screenshot(path=str(screenshot), full_page=True)
            print(screenshot)
            assert blocked == [], blocked
            page.close()

            # Without JavaScript the links are plain links to the subsystem page.
            context = browser.new_context(java_script_enabled=False)
            page = context.new_page()
            page.goto(main)
            page.locator("details#big").get_by_role(
                "link", name="All 4 postings in big"
            ).click()
            expect(page).to_have_url(base + f"{MERGE_WINDOW_PATH}/subsystems/big")
            expect(page.locator("details#big .mw-rows > ul > li")).to_have_count(4)
            context.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_front_merge_window_figures_layout(client, session, monkeypatch, tmp_path):
    from tests.test_routes.test_front_merge_window import _warm

    playwright = playwright_api()
    expect = playwright.expect
    server = make_server("127.0.0.1", 0, client.application, threaded=True)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with playwright.sync_playwright() as p, p.chromium.launch() as browser:
            for tag, status in (
                ("v7.4", "Open since v7.4"),
                ("v7.4-rc4", "Closed until v7.4 is released"),
            ):
                from mimir import cache

                for key in cache.keys():
                    if key.startswith(("merge_window:", "linus_pulls:")):
                        cache.delete(key)
                _warm(session, monkeypatch, tag=tag)
                for width in (390, 1280):
                    for theme in ("light", "dark"):
                        page = browser.new_page(
                            viewport={"width": width, "height": 900}, color_scheme=theme
                        )
                        block_external_requests(page, base)
                        page.goto(base + "/")
                        expect(page.locator(".front-merge-window-status")).to_have_text(
                            status
                        )
                        expect(
                            page.locator('[data-figure="queued-patches"]')
                        ).to_have_text("12,345")
                        expect(
                            page.locator('[data-figure="queued-subsystems"]')
                        ).to_have_text("5")
                        expect(
                            page.locator('[data-figure="pulls-merged"]')
                        ).to_have_text("3")
                        expect(
                            page.locator('[data-figure="pulls-waiting"]')
                        ).to_have_text("2")
                        expect(
                            page.locator(".front-merge-window-updated")
                        ).to_have_text("09:07 UTC")
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        )
                        shot = tmp_path / f"front-merge-{tag}-{width}-{theme}.png"
                        page.screenshot(path=str(shot), full_page=True)
                        print(shot)
                        page.locator("#merge-window a").click()
                        expect(page).to_have_url(base + "/merge-window")
                        expect(page.get_by_role("heading", level=1)).to_have_text(
                            "Linux merge window"
                        )
                        page.close()
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
