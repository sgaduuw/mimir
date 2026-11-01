"""The site nav (#988): current-page marker, and the phone menu."""

import re
from pathlib import Path

import mimir
from tests.test_routes._helpers import _ingest_one_article


def _nav(html: str) -> str:
    return re.search(r'<nav class="site-nav">(.*?)</nav>', html, re.DOTALL).group(1)


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
    assert re.search(r"\.site-nav \.nav-toggle ~ ul\s*\{[^}]*display:\s*none", rules)
    assert re.search(
        r"\.site-nav \.nav-toggle\[open\] ~ ul\s*\{[^}]*display:\s*flex", rules
    )
    # 2.75rem is 44 px at the default root size: every link and summary.
    assert re.search(
        r"\.site-nav a,\s*\.site-nav summary\s*\{[^}]*min-height:\s*2\.75rem", rules
    )
    assert re.search(
        r"@media \(width >= 768px\)\s*\{\s*\.site-nav \.nav-toggle\s*\{\s*display:\s*none",
        css,
    )


def _phone_selectors(css: str) -> list[str]:
    block = re.search(r"@media \(width < 768px\)\s*\{(.*?)\n\}", css, re.DOTALL)
    assert block, "no phone block for the nav"
    rules = re.sub(r"/\*.*?\*/", "", block.group(1), flags=re.DOTALL)
    return [
        sel.strip()
        for group in re.findall(r"([^{}]+)\{[^}]*\}", rules)
        for sel in group.split(",")
    ]


def _reaches(selector: str, nav_tag: str, nav_html: str) -> bool:
    """Whether `selector` can style something in this `<nav>`: its leading
    compound matches the nav and every other class it needs is inside it.
    Only tag and class compounds lead here; anything else fails loudly so
    the check is extended rather than silently wrong."""
    lead = re.match(r"[^\s>+~]+", selector).group(0)
    assert re.fullmatch(r"(nav)?(\.[\w-]+)*", lead), f"unhandled lead {lead!r}"
    lead_classes = set(re.findall(r"\.([\w-]+)", lead))
    rest_classes = set(re.findall(r"\.([\w-]+)", selector[len(lead) :]))
    m = re.search(r'\bclass="([^"]*)"', nav_tag)
    present = set(m.group(1).split()) if m else set()
    inside = set(re.findall(r'\bclass="([^"]*)"', nav_html))
    inside = {c for value in inside for c in value.split()}
    return lead_classes <= present and rest_classes <= inside


def test_phone_rules_reach_only_the_site_nav(client):
    """The phone block is for the site nav. A bare `nav a` also flexed the
    merge window's in-page subsystem index (each link and each "(n), " on
    its own line), and a bare `nav > ul li + li::before` put a "/" between
    the month archive's "prev" and "up" links."""
    from mimir.extensions import SessionLocal
    from tests.test_merge_window import _article, _commit, _subsystem

    with SessionLocal() as s:
        _subsystem(s, "SCOPE", ["scope/"])
        art = _article(s, "scope@x", files=["scope/a.c"])
        _commit(s, "scope@x", "linux-next")
        s.commit()
        month = f"/alpha/{art.date:%Y}/{art.date:%m}/"
    selectors = _phone_selectors(
        (Path(mimir.__file__).parent / "static/css/mimir.css").read_text()
    )
    reached = {}
    for path in ("/merge-window", month):
        html = client.get(path).get_data(as_text=True)
        navs = re.findall(r"(<nav\b[^>]*>)(.*?)</nav>", html, re.DOTALL)
        site = [n for n in navs if 'class="site-nav"' in n[0]]
        others = [n for n in navs if 'class="site-nav"' not in n[0]]
        assert len(site) == 1, f"{path}: expected one site nav"
        assert others, f"{path}: precondition, no in-page nav to protect"
        # The matcher can say yes: the phone block does reach the site nav.
        assert any(_reaches(sel, *site[0]) for sel in selectors)
        reached[path] = sorted(
            {
                sel
                for tag, body in others
                for sel in selectors
                if _reaches(sel, tag, body)
            }
        )
    assert reached == {"/merge-window": [], month: []}
