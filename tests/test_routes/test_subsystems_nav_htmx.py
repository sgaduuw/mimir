"""#892: the nav dropdown lists "the current message's subsystems". A click
in the message page's thread tree swaps only `<article id="msg">`
(message.html: hx-target="#msg", hx-swap="outerHTML", hx-push-url), so
whatever the nav shows must still describe the message on screen after
that swap. `_htmx_swap` applies the response the way htmx does for these
links: elements marked `hx-swap-oob="true"` replace the element with the
same id, and the rest replaces #msg."""

import re

from tests.test_routes._helpers import _ingest_one_article, _seed_subsystem

_DD = re.compile(r'<ul id="subsystems-dd-menu"[^>]*>(.*?)</ul>', re.DOTALL)
_OPEN = re.compile(r"<(\w+)\b([^>]*)>")


def _element_at(html: str, start: int) -> tuple[int, int]:
    """(start, end) of the element whose start tag begins at `start`."""
    tag = _OPEN.match(html, start).group(1)
    depth = 0
    for m in re.finditer(rf"<(/?){tag}\b[^>]*>", html[start:]):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return start, start + m.end()
    raise AssertionError(f"unclosed <{tag}>")


def _by_id(html: str, el_id: str) -> tuple[int, int]:
    m = re.search(rf'<\w+\b[^>]*\bid="{re.escape(el_id)}"', html)
    assert m, f"no element with id={el_id!r}"
    return _element_at(html, m.start())


def _htmx_swap(page: str, response: str, target_id: str = "msg") -> str:
    for m in list(re.finditer(r'<\w+\b[^>]*\bhx-swap-oob="true"[^>]*>', response))[
        ::-1
    ]:
        s, e = _element_at(response, m.start())
        oob = response[s:e]
        el_id = re.search(r'\bid="([^"]+)"', oob).group(1)
        ps, pe = _by_id(page, el_id)
        page = page[:ps] + oob + page[pe:]
        response = response[:s] + response[e:]
    ts, te = _by_id(page, target_id)
    return page[:ts] + response.strip() + page[te:]


def _patch(d: str) -> bytes:
    return f"diff --git a/{d}/x.c b/{d}/x.c\n@@ -1 +1 @@\n-x\n+y\n".encode()


def test_nav_dropdown_follows_an_in_thread_htmx_swap(client, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _seed_subsystem("XFS", "Supported", files=["fs/xfs/"])
    _seed_subsystem("BCACHEFS", "Supported", files=["fs/bcachefs/"])
    _, url_a = _ingest_one_article(
        tmp_path / "a", "alpha", "dd-a@example.com", body=_patch("fs/xfs")
    )
    page = client.get(url_a).get_data(as_text=True)
    assert "/alpha/subsystem/xfs/" in _DD.search(page).group(1)  # precondition

    _, url_b = _ingest_one_article(
        tmp_path / "b",
        "alpha",
        "dd-b@example.com",
        in_reply_to="dd-a@example.com",
        body=_patch("fs/bcachefs"),
    )
    partial = client.get(url_b, headers={"HX-Request": "true"}).get_data(as_text=True)
    assert "/alpha/subsystem/bcachefs/" in partial  # B's own header is in it

    menu = _DD.search(_htmx_swap(page, partial)).group(1)
    assert "/alpha/subsystem/xfs/" not in menu, (
        "nav still lists the previous message's subsystems"
    )
    assert "/alpha/subsystem/bcachefs/" in menu


def test_phone_menu_holds_the_one_target_the_partial_refreshes(client, tmp_path):
    """#988: on phones the subsystems menu lives inside the collapsible nav
    menu. The swap only works if the page has exactly one element with the
    partial's out-of-band id, and that element sits in the list the menu
    toggle opens, so the refresh lands there on phone and desktop alike."""
    _, url = _ingest_one_article(tmp_path, "alpha", "dd-phone@example.com")
    page = client.get(url).get_data(as_text=True)
    assert re.findall(r'\bid="subsystems-dd-menu"', page) == ['id="subsystems-dd-menu"']

    # `.nav-toggle[open] ~ ul` reaches only siblings: following the toggle
    # in document order is not enough, a wrapper would leave it unopenable.
    menu = re.search(
        r'<details class="nav-toggle">.*?</details>\s*(<ul class="nav-menu">)',
        page,
        re.DOTALL,
    )
    assert menu, "the menu list does not directly follow the toggle"
    ms, me = _element_at(page, menu.start(1))
    ts, _ = _by_id(page, "subsystems-dd-menu")
    assert ms < ts < me, "the OOB target is outside the phone menu"
