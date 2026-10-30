"""The site nav (#988): current-page marker, and the phone menu."""

import re
from pathlib import Path

import mimir
from tests.test_routes._helpers import _ingest_one_article


def _nav(html: str) -> str:
    return re.search(r"<nav>(.*?)</nav>", html, re.DOTALL).group(1)


def test_nav_marks_only_the_link_to_the_current_page(client, tmp_path):
    """aria-current="page" says "this link is the page you are on", so it
    goes on the link whose href is the path, and nowhere on a page the nav
    does not link to (a message page)."""
    _, msg_url = _ingest_one_article(tmp_path, "alpha", "nav-cur@example.com")
    linked = ("/", "/alpha/", "/alpha/today", "/alpha/yesterday", "/merge-window")
    current = {
        path: re.findall(
            r'<a href="([^"]+)"[^>]*\baria-current="page"',
            _nav(client.get(path).get_data(as_text=True)),
        )
        for path in (*linked, msg_url)
    }
    assert current == {**{p: [p] for p in linked}, msg_url: []}


def test_no_menu_toggle_outside_an_inbox(client, tmp_path):
    """Outside an inbox the nav is the site name and Merge window, which fit
    on a phone, so a toggle there would hide the one link behind a tap."""
    _ingest_one_article(tmp_path, "alpha", "nav-tog@example.com")
    assert "nav-toggle" not in _nav(client.get("/").get_data(as_text=True))
    assert "nav-toggle" in _nav(client.get("/alpha/").get_data(as_text=True))


def test_css_collapses_the_nav_on_phones_with_44px_targets():
    css = (Path(mimir.__file__).parent / "static/css/mimir.css").read_text()
    phone = re.search(r"@media \(width < 768px\)\s*\{(.*?)\n\}", css, re.DOTALL)
    assert phone, "no phone block for the nav"
    rules = phone.group(1)
    assert re.search(r"nav \.nav-toggle ~ ul\s*\{[^}]*display:\s*none", rules)
    # The open rows start where the breadcrumb does: Pico's li padding is
    # cancelled the same way Pico cancels it on the first list.
    assert re.search(
        r"nav \.nav-toggle ~ ul\s*\{[^}]*margin-inline:\s*"
        r"calc\(var\(--pico-nav-element-spacing-horizontal\) \* -1\)",
        rules,
    )
    assert re.search(r"nav \.nav-toggle\[open\] ~ ul\s*\{[^}]*display:\s*flex", rules)
    # 2.75rem is 44 px at the default root size: every link and summary.
    assert re.search(r"nav a,\s*nav summary\s*\{[^}]*min-height:\s*2\.75rem", rules)
    assert re.search(
        r"@media \(width >= 768px\)\s*\{\s*nav \.nav-toggle\s*\{\s*display:\s*none",
        css,
    )
