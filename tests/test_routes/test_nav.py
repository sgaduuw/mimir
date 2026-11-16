"""The site nav (#988): two rows, the current-page marker, and phones."""

import re
from pathlib import Path

import mimir
from tests.test_routes._helpers import _ingest_one_article


def _nav(html: str) -> str:
    return re.search(r'<nav class="site-nav">(.*?)</nav>', html, re.DOTALL).group(1)


def _inbox_nav(html: str) -> str | None:
    m = re.search(r'<nav class="inbox-nav"[^>]*>(.*?)</nav>', html, re.DOTALL)
    return m.group(1) if m else None


def _current(html: str) -> list[str]:
    navs = _nav(html) + (_inbox_nav(html) or "")
    return re.findall(r'<a href="([^"]+)"[^>]*\baria-current="page"', navs)


def test_nav_marks_only_the_link_to_the_current_page(client, tmp_path):
    """aria-current="page" says "this link is the page you are on", so it
    goes on the link whose href is the path, in either row, and nowhere on
    a page the nav does not link to (a message page)."""
    _, msg_url = _ingest_one_article(tmp_path, "alpha", "nav-cur@example.com")
    linked = (
        "/",
        "/subsystems/",
        "/maintainers/",
        "/merge-window",
        "/alpha/",
        "/alpha/today",
        "/alpha/yesterday",
    )
    current = {
        path: _current(client.get(path).get_data(as_text=True))
        for path in (*linked, msg_url)
    }
    assert current == {**{p: [p] for p in linked}, msg_url: []}


def test_a_section_marks_the_pages_under_it_without_claiming_them(client):
    """The release history sits under Releases. Its nav link is marked as
    the section, by class, and is not aria-current: it is not this page."""
    html = client.get("/merge-window/history").get_data(as_text=True)
    assert re.findall(r'<a href="/merge-window"[^>]*>', _nav(html)) == [
        '<a href="/merge-window" class="nav-section">'
    ]
    assert _current(html) == []


def test_sections_are_on_every_page_and_the_list_row_only_in_an_inbox(client, tmp_path):
    """Readers land on a thread from a search engine (#891), so the site's
    sections are in the top row everywhere. The list's own row appears
    only inside an inbox, and neither row hides behind a menu toggle."""
    _, msg_url = _ingest_one_article(tmp_path, "alpha", "nav-tog@example.com")
    rows = {}
    for path in ("/", "/merge-window", "/alpha/", msg_url):
        html = client.get(path).get_data(as_text=True)
        sections = re.search(
            r'<ul class="nav-sections">(.*?)</ul>', _nav(html), re.DOTALL
        ).group(1)
        inbox = _inbox_nav(html)
        rows[path] = (
            re.findall(r">([A-Z][a-z]+)</a>", sections),
            re.findall(r'<a href="([^"]+)"', inbox) if inbox else None,
            "nav-toggle" in html,
        )
    labels = ["Subsystems", "Maintainers", "Releases"]
    inbox_row = ["/alpha/", "/alpha/today", "/alpha/yesterday"]
    assert {p: (r[0], r[1][:3] if r[1] else None, r[2]) for p, r in rows.items()} == {
        "/": (labels, None, False),
        "/merge-window": (labels, None, False),
        "/alpha/": (labels, inbox_row, False),
        msg_url: (labels, inbox_row, False),
    }


def test_css_gives_phones_a_section_line_and_44px_targets():
    css = (Path(mimir.__file__).parent / "static/css/mimir.css").read_text()
    phone = re.search(r"@media \(width < 768px\)\s*\{(.*?)\n\}", css, re.DOTALL)
    assert phone, "no phone block for the nav"
    rules = phone.group(1)
    # The sections drop to a full-width line of their own under the name.
    assert re.search(
        r"\.site-nav \.nav-sections\s*\{[^}]*order:\s*1;[^}]*flex:\s*1 0 100%", rules
    )
    # 2.75rem is 44 px at the default root size: every link and summary.
    assert re.search(
        r"\.site-nav a,\s*\.inbox-nav a,\s*\.inbox-nav summary\s*"
        r"\{[^}]*min-height:\s*2\.75rem",
        rules,
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
    """The phone block is for the two nav rows. A bare `nav a` once flexed the
    merge window's in-page subsystem index (since replaced by a list of
    disclosures, #1000), and a bare `nav > ul li + li::before` put a "/"
    between the month archive's "prev" and "up" links. The privacy page's
    contents list is an in-page nav too (#1001)."""
    from tests.test_routes._helpers import _any_article_in

    art = _any_article_in("alpha")
    month = f"/alpha/{art.date:%Y}/{art.date:%m}/"
    selectors = _phone_selectors(
        (Path(mimir.__file__).parent / "static/css/mimir.css").read_text()
    )
    reached = {}
    for path in (month, "/privacy"):
        html = client.get(path).get_data(as_text=True)
        navs = re.findall(r"(<nav\b[^>]*>)(.*?)</nav>", html, re.DOTALL)
        ours = ('class="site-nav"', 'class="inbox-nav"')
        site = [n for n in navs if 'class="site-nav"' in n[0]]
        others = [n for n in navs if not any(c in n[0] for c in ours)]
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
    assert reached == {month: [], "/privacy": []}
