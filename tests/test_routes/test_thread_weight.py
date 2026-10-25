"""The thread view renders lighter than the message page (#814).

Same article, two surfaces: the message page keeps per-token highlighting
and per-line anchors, the thread view drops both and folds long plain-text
blocks. Everything is asserted on parsed HTML, not substrings, so a class
name inside escaped patch text cannot satisfy or break a check.
"""

import hashlib
from html.parser import HTMLParser

from mimir.rendering import render_body
from mimir.rendering.body import BODY_TEXT_FOLD_LINES
from tests.test_routes._helpers import _seed_three_message_thread

_PATCH = (
    "Fix the thing.\n"
    "\n"
    "Signed-off-by: A Dev <dev@example.org>\n"
    "---\n"
    "diff --git a/drivers/foo.c b/drivers/foo.c\n"
    "index 1111111..2222222 100644\n"
    "--- a/drivers/foo.c\n"
    "+++ b/drivers/foo.c\n"
    "@@ -1,4 +1,5 @@\n"
    " static int foo(void)\n"
    " {\n"
    "-\treturn 0;\n"
    "+\tint ret = 1;\n"
    "+\treturn ret;\n"
    " }\n"
    "@@ -20,3 +21,3 @@\n"
    " /* comment */\n"
    "-#define A 1\n"
    "+#define A 2\n"
    "-- \n"
    "2.43.0\n"
)


def _log(lines: int) -> str:
    return "\n".join(f"[{i:04d}] log line {i}" for i in range(lines)) + "\n"


class _Page(HTMLParser):
    """Collects what the checks need from one rendered fragment."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.diff_spans: list[dict] = []  # <span> inside <div class=highlight>
        self.details = 0
        self.pre_text: list[str] = []
        self._in_highlight = 0
        self._div_stack: list[bool] = []
        self._pre_buf: list[str] | None = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "div":
            hl = "highlight" in (a.get("class") or "").split()
            self._div_stack.append(hl)
            self._in_highlight += hl
        if tag == "span" and self._in_highlight:
            self.diff_spans.append(a)
        if tag == "details":
            self.details += 1
        if tag == "pre" and "body-text-block" in (a.get("class") or ""):
            self._pre_buf = []

    def handle_endtag(self, tag):
        if tag == "div" and self._div_stack:
            self._in_highlight -= self._div_stack.pop()
        if tag == "pre" and self._pre_buf is not None:
            self.pre_text.append("".join(self._pre_buf))
            self._pre_buf = None

    def handle_data(self, data):
        if self._pre_buf is not None:
            self._pre_buf.append(data)

    @property
    def token_spans(self):
        """Pygments token spans: a class that is not a line-level marker."""
        return [
            s
            for s in self.diff_spans
            if s.get("class") not in (None, "gi", "gd", "gu", "gh")
        ]


def test_message_page_rendering_is_unchanged():
    """Pinned digest of the default rendering of a patch plus a long log,
    taken before #814 touched anything. The message page must not move."""
    out = str(render_body(_PATCH + "\n" + _log(100)))
    assert hashlib.sha256(out.encode()).hexdigest() == (
        "35c36b9bd23378c97faa826040ab98bdfebd84e72f3c6bc76d5b6caa6a75c96b"
    )


def test_compact_diff_has_no_token_spans_and_the_default_has():
    full = _Page(str(render_body(_PATCH)))
    compact = _Page(str(render_body(_PATCH, compact=True)))
    assert full.token_spans, "fixture produced no token spans to remove"
    assert compact.token_spans == []
    # The line-level classes survive: +/- markers and @@ headers.
    classes = {s.get("class") for s in compact.diff_spans}
    assert {"gi", "gd", "gu"} <= classes


def test_compact_diff_has_no_anchor_ids_and_the_default_has():
    assert any(i.startswith("h-") for i in _Page(str(render_body(_PATCH))).ids)
    assert _Page(str(render_body(_PATCH, compact=True))).ids == []


_MORE = ("/inbox/2024/01/9", "/api/inbox/body/9")


class _Links(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.more: list[dict] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and "hx-get" in a:
            self.more.append(a)


def test_compact_cuts_a_long_text_block_and_links_to_the_rest():
    n = BODY_TEXT_FOLD_LINES + 60
    out = str(render_body(_log(n), compact=True, text_more=_MORE))
    page = _Page(out)
    assert len(page.pre_text) == 1
    assert page.pre_text[0].count("\n") + 1 == BODY_TEXT_FOLD_LINES
    assert f"log line {BODY_TEXT_FOLD_LINES}\n" not in out + "\n"
    (link,) = _Links(out).more
    assert link["href"] == _MORE[0]
    assert link["hx-get"] == _MORE[1]


def test_compact_without_text_more_does_not_cut():
    n = BODY_TEXT_FOLD_LINES + 60
    page = _Page(str(render_body(_log(n), compact=True)))
    assert page.pre_text[0].split("\n") == _log(n).rstrip("\n").split("\n")


def test_compact_leaves_a_short_text_block_alone():
    out = str(render_body(_log(BODY_TEXT_FOLD_LINES), compact=True, text_more=_MORE))
    assert _Links(out).more == []
    assert len(_Page(out).pre_text) == 1


def test_compact_is_much_smaller_on_a_diff_heavy_message():
    # The trailer ends a diff block, so repeat the part before it.
    body = _PATCH.split("-- \n")[0] * 40
    full = len(str(render_body(body)))
    compact = len(str(render_body(body, compact=True)))
    assert full / compact >= 1.8, f"only {full / compact:.2f}x smaller"


def test_compact_is_much_smaller_on_a_log_heavy_message():
    body = _log(3000)
    full = len(str(render_body(body)))
    compact = len(str(render_body(body, compact=True, text_more=_MORE)))
    assert full / compact >= 20, f"only {full / compact:.2f}x smaller"


def _quote(lines: int) -> str:
    """A syzbot-shaped quote: nested levels, `lines` lines in all."""
    return "\n".join(
        ">" * (1 + i % 3) + f" earlier report line {i}" for i in range(lines)
    )


def _parse_calls(monkeypatch) -> list[int]:
    from mimir.rendering import body as body_mod

    calls: list[int] = []
    real = body_mod.parse_blocks

    def counting(text):
        calls.append(1)
        return real(text)

    monkeypatch.setattr(body_mod, "parse_blocks", counting)
    return calls


def test_compact_cuts_a_long_quote_and_links_to_the_rest():
    n = BODY_TEXT_FOLD_LINES + 60
    out = str(render_body("reply\n\n" + _quote(n), compact=True, text_more=_MORE))
    assert f"earlier report line {BODY_TEXT_FOLD_LINES - 1}" in out
    assert f"earlier report line {BODY_TEXT_FOLD_LINES}" not in out
    assert f"earlier report line {n - 1}" not in out
    (link,) = _Links(out).more
    assert (link["href"], link["hx-get"]) == _MORE
    assert "60 more quoted lines" in out


def test_compact_does_not_parse_the_cut_part_of_a_quote(monkeypatch):
    calls = _parse_calls(monkeypatch)
    render_body(_quote(900), compact=True, text_more=_MORE)
    cut = len(calls)
    calls.clear()
    render_body(_quote(900), compact=True)
    assert len(calls) > 5 * cut, f"cut {cut} parse_blocks calls, uncut {len(calls)}"


def test_compact_leaves_a_short_quote_alone():
    q = _quote(BODY_TEXT_FOLD_LINES)
    cut = str(render_body(q, compact=True, text_more=_MORE))
    assert cut == str(render_body(q, compact=True))
    assert _Links(cut).more == []


def test_compact_is_much_smaller_on_a_syzbot_shaped_message():
    body = "report\n\n" + _quote(800)
    full = len(str(render_body(body)))
    compact = len(str(render_body(body, compact=True, text_more=_MORE)))
    assert full / compact >= 10, f"only {full / compact:.2f}x smaller"


def test_thread_page_ids_are_distinct_and_message_page_keeps_its_anchors(
    client, tmp_path
):
    seeded = _seed_three_message_thread(
        tmp_path, "alpha", bodies=(_PATCH.encode(),) * 3
    )
    _, root_url, _ = seeded["root"]

    thread = _Page(client.get(root_url + "/t").get_data(as_text=True))
    assert len(thread.ids) == len(set(thread.ids)), "duplicate ids on thread page"
    assert not [i for i in thread.ids if i.startswith("h-")]
    assert thread.token_spans == []

    message = _Page(client.get(root_url).get_data(as_text=True))
    assert "h-1-L2" in message.ids
    assert message.token_spans


def test_thread_page_cuts_a_long_log_and_the_fragment_has_all_of_it(client, tmp_path):
    n = BODY_TEXT_FOLD_LINES + 60
    seeded = _seed_three_message_thread(
        tmp_path, "alpha", bodies=(_log(n).encode(), b"body", b"body")
    )
    root_id, root_url, _ = seeded["root"]

    html = client.get(root_url + "/t").get_data(as_text=True)
    assert f"log line {n - 1}\n" not in html
    (link,) = _Links(html).more
    assert link["href"] == root_url
    assert link["hx-get"] == f"/api/alpha/body/{root_id}"

    frag = client.get(link["hx-get"])
    assert frag.status_code == 200
    assert frag.headers["X-Robots-Tag"] == "noindex"
    page = _Page(frag.get_data(as_text=True))
    assert page.pre_text[0].split("\n") == _log(n).rstrip("\n").split("\n")

    message = _Page(client.get(root_url).get_data(as_text=True))
    assert len(message.pre_text[0].split("\n")) >= n


def test_fragment_validator_revalidates_and_tracks_its_inputs(
    client, tmp_path, monkeypatch
):
    from mimir.config import settings

    seeded = _seed_three_message_thread(tmp_path, "alpha")
    root_id, _, _ = seeded["root"]
    url = f"/api/alpha/body/{root_id}"

    first = client.get(url)
    etag = first.headers["ETag"]
    assert client.get(url, headers={"If-None-Match": etag}).status_code == 304

    monkeypatch.setattr(settings, "email_allowlist", ["someone@example.org"])
    assert client.get(url, headers={"If-None-Match": etag}).status_code == 200


def test_fragment_404s_outside_the_inbox(client, tmp_path):
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    root_id, _, _ = seeded["root"]
    assert client.get(f"/api/alpha/body/{root_id}").status_code == 200
    # Exists, but only in alpha.
    assert client.get(f"/api/beta/body/{root_id}").status_code == 404
    assert client.get(f"/api/alpha/body/{root_id + 1000}").status_code == 404
    assert client.get(f"/api/nosuchinbox/body/{root_id}").status_code == 404


def test_thread_page_cuts_a_long_quote_and_the_fragment_has_all_of_it(client, tmp_path):
    n = BODY_TEXT_FOLD_LINES + 60
    seeded = _seed_three_message_thread(
        tmp_path, "alpha", bodies=(("hi\n\n" + _quote(n)).encode(), b"b", b"b")
    )
    _, root_url, _ = seeded["root"]

    html = client.get(root_url + "/t").get_data(as_text=True)
    assert f"earlier report line {n - 1}" not in html
    (link,) = _Links(html).more
    frag = client.get(link["hx-get"]).get_data(as_text=True)
    assert f"earlier report line {n - 1}" in frag
    assert _Links(frag).more == []
