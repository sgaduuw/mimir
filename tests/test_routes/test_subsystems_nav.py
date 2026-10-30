"""The "subsystems" dropdown in the inbox nav (#892)."""

import re
from pathlib import Path

import mimir
from tests.test_routes._helpers import _ingest_one_article, _seed_subsystem

_DD = re.compile(r'<ul id="subsystems-dd-menu"[^>]*>(.*?)</ul>', re.DOTALL)
_HREF = re.compile(r'<li><a href="([^"]+)"')


def _dropdown_hrefs(html: str) -> list[str]:
    m = _DD.search(html)
    assert m, "no subsystems dropdown in the page"
    return _HREF.findall(m.group(1))


def _patch(*dirs: str) -> bytes:
    return b"".join(
        f"diff --git a/{d}/x.c b/{d}/x.c\n@@ -1 +1 @@\n-x\n+y\n".encode() for d in dirs
    )


def test_dropdown_present_in_an_inbox_and_absent_outside_one(client, tmp_path):
    _, url = _ingest_one_article(tmp_path, "alpha", "nav-a@example.com")
    assert _dropdown_hrefs(client.get("/alpha/").get_data(as_text=True)) == [
        "/alpha/subsystem/"
    ]
    assert _DD.search(client.get("/").get_data(as_text=True)) is None


def test_message_page_lists_its_subsystems_then_all_subsystems(client, tmp_path):
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    _seed_subsystem("XFS", "Supported", files=["fs/xfs/"])
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "nav-b@example.com",
        body=_patch("fs/bcachefs", "fs/xfs"),
    )
    html = client.get(url).get_data(as_text=True)
    hrefs = _dropdown_hrefs(html)
    assert hrefs == [
        "/alpha/subsystem/bcachefs/",
        "/alpha/subsystem/xfs/",
        "/alpha/subsystem/",
    ]
    # The header's own links are the same bytes.
    for h in hrefs[:-1]:
        assert html.count(f'<a href="{h}">') == 2


def test_dropdown_caps_at_eight_subsystems(client, tmp_path):
    names = [f"SUB{i:02d}" for i in range(10)]
    for n in names:
        _seed_subsystem(n, "Supported", files=[f"d/{n.lower()}/"])
    _, url = _ingest_one_article(
        tmp_path,
        "alpha",
        "nav-c@example.com",
        body=_patch(*[f"d/{n.lower()}" for n in names]),
    )
    html = client.get(url).get_data(as_text=True)
    # Precondition: the header really carries all ten.
    assert html.count('<a href="/alpha/subsystem/sub09/">') == 1
    hrefs = _dropdown_hrefs(html)
    assert hrefs == [f"/alpha/subsystem/sub{i:02d}/" for i in range(8)] + [
        "/alpha/subsystem/"
    ]


def test_message_without_subsystems_shows_only_all_subsystems(client, tmp_path):
    _, url = _ingest_one_article(tmp_path, "alpha", "nav-d@example.com")
    html = client.get(url).get_data(as_text=True)
    assert _dropdown_hrefs(html) == ["/alpha/subsystem/"]


def test_css_pins_hover_open_and_link_like_summary():
    css = (Path(mimir.__file__).parent / "static/css/mimir.css").read_text()
    # Not in the phone menu (#988): there the panel opens in place, so a
    # hover would shift the rows below it under the pointer.
    assert re.search(
        r"@media \(hover: hover\) and \(width >= 768px\)\s*\{\s*"
        r"nav \.subsystems-dd:hover > ul\s*"
        r"\{\s*display:\s*flex;",
        css,
    )
    # Tap and keyboard: the open <details> shows its sibling menu.
    assert "nav .subsystems-dd details[open] ~ ul { display: flex; }" in css
    summary = re.search(
        r"nav \.subsystems-dd details\.dropdown > summary,\s*"
        r"nav \.subsystems-dd details\.dropdown > summary:not\(\[role\]\)\s*\{([^}]*)\}",
        css,
    )
    assert summary
    assert "padding: 0" in summary.group(1)
    assert "var(--pico-primary)" in summary.group(1)
    # The panel starts at the label's bottom edge: no gap for hover to drop in.
    assert re.search(r"nav \.subsystems-dd > ul\s*\{[^}]*top:\s*calc\(100% - ", css)
    # The separator only exists when links precede "all subsystems".
    assert ":not(:only-child):last-child" in css
