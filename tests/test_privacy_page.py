"""`/privacy` (#1001): the notice's wording is fixed, its presentation is not.

The legal text is held word for word by a golden file, so an edit to a
sentence shows up as a diff in a data file a reviewer reads, not buried in
markup. The "In short" box is pinned too: it summarises the notice and must
not say more than the sections below it do.
"""

import re
from pathlib import Path

import mimir
from tests._dom import Node, parse

GOLDEN = Path(__file__).parent / "data" / "privacy_legal_text.txt"

IN_SHORT = [
    "No accounts, no analytics, no tracking pixels, no advertising.",
    "Three strictly necessary Cloudflare cookies and two local preferences (theme and thread fold).",
    "Request logs are kept for two days, for abuse triage and debugging.",
    "Questions or requests: the privacy contact address.",
]

SECTION_IDS = [
    "controller",
    "what-it-is",
    "browser-storage",
    "server-side",
    "third-parties",
    "redaction",
    "your-rights",
    "authority",
    "changes",
]

COOKIES = [
    ("__cf_bm", "Cloudflare", "bot mitigation"),
    (
        "cf_use_ob",
        "Cloudflare",
        "origin-failover, the Cloudflare “Always Online” feature",
    ),
    (
        "cf_ob_info",
        "Cloudflare",
        "origin-failover, the Cloudflare “Always Online” feature",
    ),
]
LOCAL = [
    (
        "mimir.theme",
        "mimir",
        "your light or dark theme preference. Written when you choose Light or Dark; choosing System removes it. Stays in your browser, never sent to the server.",
    ),
    (
        "mimir.fold.<id>",
        "mimir",
        "your fold-state preference for that thread’s reply tree. Written only when you click the fold toggle. Stays in your browser, never sent to the server.",
    ),
]
SESSION = [
    (
        "htmx-history-cache",
        "htmx",
        "copies of up to ten recently viewed pages, so the back button can show them without loading them again. Kept only for the open tab and cleared when it closes. Never sent to the server.",
    ),
]


# The legal text as a list of blocks, for the golden check: headings,
# paragraphs and list items of the notice, in order, whitespace normalised.
# Skipped: the title block (its date changes at each release), the
# navigation, and the "In short" box, which are presentation and a summary,
# not the notice itself.
_KEEP = {"h2", "p", "li"}
_SKIP_TAGS = {"hgroup", "nav"}
_SKIP_CLASSES = {"privacy-short"}


def legal_blocks(root: Node) -> list[str]:
    privacy = root.find_all(cls="privacy")
    assert len(privacy) == 1, "one .privacy root"
    out: list[str] = []

    def walk(node: Node) -> None:
        for child in node.children:
            if not isinstance(child, Node):
                continue
            if child.tag in _SKIP_TAGS or _SKIP_CLASSES & set(child.classes):
                continue
            if child.tag in _KEEP:
                out.append(child.text())
            else:
                walk(child)

    walk(privacy[0])
    return out


def _page(client):
    return parse(client.get("/privacy").get_data(as_text=True))


def test_the_legal_text_is_word_for_word(client):
    """Every heading, paragraph and list item of the notice, apart from the
    four storage list items that became table rows (see the table test)."""
    expected = GOLDEN.read_text().splitlines()
    assert len(expected) > 30  # the golden was read
    assert legal_blocks(_page(client)) == expected


def test_in_short_is_the_approved_wording_and_leads_the_article(client):
    page = _page(client)
    box = page.find("section", cls="privacy-short")
    assert [li.text() for li in box.find_all("li")] == IN_SHORT
    article = page.find("article", cls="privacy-article")
    headings = article.find_all("h2")
    assert headings[0].text() == "In short"
    assert headings[1].attrs["id"] == "controller"


def test_the_contact_address_is_the_one_in_the_controller_section(client):
    page = _page(client)
    box = page.find("section", cls="privacy-short")
    in_box = [a.attrs["href"] for a in box.find_all("a")]
    everywhere = [
        a.attrs["href"]
        for a in page.find("article", cls="privacy-article").find_all("a")
        if a.attrs["href"].startswith("mailto:")
    ]
    controller = everywhere[1:]  # the box comes first in the article
    assert len(everywhere) == 3
    assert len(in_box) == 1 and in_box[0].startswith("mailto:")
    assert in_box[0] == controller[0]


def test_the_contents_list_and_the_headings_share_one_source(client):
    page = _page(client)
    toc = page.find("nav", cls="privacy-toc")
    links = toc.find_all("a")
    assert [a.attrs["href"] for a in links] == [f"#{i}" for i in SECTION_IDS]
    article = page.find("article", cls="privacy-article")
    by_id = {
        h.attrs["id"]: h.text()
        for h in article.find_all("h2")
        if h.attrs.get("id") != "in-short"
    }
    assert list(by_id) == SECTION_IDS
    # The link text is the heading text: no second copy to drift.
    assert [a.text() for a in links] == [by_id[i] for i in SECTION_IDS]


def _table_rows(table):
    head = [th.text() for th in table.find("thead").find_all("th")]
    assert all(th.attrs["scope"] == "col" for th in table.find("thead").find_all("th"))
    rows = []
    for tr in table.find("tbody").find_all("tr"):
        header = tr.find_all("th")
        assert len(header) == 1 and header[0].attrs["scope"] == "row"
        cells = [c.text() for c in tr.find_all("td")]
        rows.append((header[0].text(), *cells))
    return head, rows


def test_storage_is_three_real_tables_with_the_notices_own_words(client):
    page = _page(client)
    tables = page.find("article", cls="privacy-article").find_all("table")
    assert len(tables) == 3
    cookies, local, session = tables
    for table, rows in ((cookies, COOKIES), (local, LOCAL), (session, SESSION)):
        head, found = _table_rows(table)
        assert head == ["Name", "Set by", "Purpose"]
        assert found == rows
    # Each table sits under the sentence that introduces it, the "strictly
    # necessary" paragraph keeps its place between the first two, and the
    # session-storage table comes before "No analytics".
    order = []
    for node in page.find("article", cls="privacy-article").iter():
        if node.tag == "table":
            order.append("table")
        elif node.tag == "p":
            for label in (
                "Three Cloudflare",
                "All three",
                "Local storage",
                "Session storage",
                "No analytics",
            ):
                if node.text().startswith(label):
                    order.append(label)
    assert order == [
        "Three Cloudflare",
        "table",
        "All three",
        "Local storage",
        "table",
        "Session storage",
        "table",
        "No analytics",
    ]


def test_every_local_storage_key_the_scripts_write_is_in_the_notice(client):
    """The class guard: a new `localStorage` key in any script must reach
    this page. It would have caught the stale "one local fold preference"."""
    keys = set()
    scripts = 0
    for path in sorted((Path(mimir.__file__).parent / "static" / "js").glob("*.js")):
        scripts += 1
        keys.update(
            re.findall(
                r"""(?:local|session)Storage\.(?:get|set|remove)Item\(\s*["'`]([^"'`]+)""",
                path.read_text(),
            )
        )
    assert scripts >= 5
    assert keys == {"mimir.theme", "mimir.fold."}
    page_text = _page(client).text()
    assert [k for k in sorted(keys) if k not in page_text] == []


def test_the_reading_column_is_capped():
    css = (Path(mimir.__file__).parent / "static" / "css" / "mimir.css").read_text()
    assert re.search(r"\.privacy-article\s*\{[^}]*max-width:\s*68ch", css)
