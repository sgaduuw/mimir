"""The shared breadcrumb (_breadcrumb.html) and the `h` key's target.

The trail and the key both answer "what is this page's parent?". They
read one list, so the test is that they give the same answer on every
page that sets one, and that the answer is the expected page.
"""

import re

import pytest

from mimir.models import Subsystem, SubsystemMaintainer
from mimir.subsystems import subsystem_path


@pytest.fixture
def subsystem(session):
    sub = Subsystem(name="ALPHA DRIVER", status="Maintained")
    session.add(sub)
    session.flush()
    session.add(
        SubsystemMaintainer(
            subsystem_id=sub.id, role="M", name="Alice", address="alice@kernel.org"
        )
    )
    session.commit()
    return sub


def _trail(html: str) -> list[tuple[str, str | None]]:
    """(label, href) per crumb, href None for an unlinked one."""
    nav = re.search(
        r'<nav aria-label="breadcrumb"><ul>(.*?)</ul></nav>', html, re.DOTALL
    )
    assert nav, "no breadcrumb on the page"
    crumbs = []
    for li in re.findall(r"<li\b[^>]*>(.*?)</li>", nav.group(1), re.DOTALL):
        link = re.fullmatch(r'<a href="([^"]*)">(.*)</a>', li)
        crumbs.append((link.group(2), link.group(1)) if link else (li, None))
    return crumbs


def _nav_up(html: str) -> str | None:
    m = re.search(r'<html\b[^>]*\bdata-nav-up="([^"]*)"', html)
    return m.group(1) if m else None


def test_the_h_key_goes_where_the_breadcrumb_says(client, subsystem, monkeypatch):
    from mimir.config import settings

    monkeypatch.setattr(settings, "site_name", "Archive Under Test")
    dashboard = subsystem_path("alpha", subsystem.name)
    expected = {
        "/merge-window": "/",
        "/merge-window/history": "/merge-window",
        "/subsystems/": "/",
        "/subsystems/a/": "/subsystems/",
        "/maintainers/": "/",
        "/maintainers/a/": "/maintainers/",
        "/maintainers/alice@kernel.org": "/maintainers/",
        dashboard: "/alpha/subsystems/",
    }
    seen = {}
    for path in expected:
        response = client.get(path)
        assert response.status_code == 200, path
        html = response.get_data(as_text=True)
        trail = _trail(html)
        linked = [href for _, href in trail[:-1] if href]
        seen[path] = (_nav_up(html), linked[-1])
        # Site sections start at the configured name, never a hard-coded
        # one (the directories said "Ratatoskr" whatever site_name was).
        if not path.startswith("/alpha/"):
            assert trail[0] == ("Archive Under Test", "/"), path
        assert "Ratatoskr" not in str(trail), path
    assert seen == {path: (up, up) for path, up in expected.items()}


def test_the_last_crumb_is_the_page_and_not_a_link(client, subsystem):
    html = client.get("/maintainers/alice@kernel.org").get_data(as_text=True)
    assert _trail(html)[-1] == ("Alice", None)
    assert '<li aria-current="page">Alice</li>' in html
