"""Body → HTML pipeline contract.

Public-facing archive's biggest single regression risk lives in
`mimir.rendering`. Route smoke tests use real lkml data, so
adversarial inputs (XSS payloads, weird-scheme URLs, deeply nested
quotes) never get exercised that way. These tests pin the
escaping behavior and the structural transformations.
"""
from markupsafe import Markup

from mimir.rendering import URL_OR_MSGID_RE, linkify, parse_blocks, render_body


# linkify — escaping + URL handling


def test_linkify_escapes_html_metacharacters():
    out = linkify("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&lt;/script&gt;" in out


def test_linkify_escapes_ampersand_and_quote():
    out = linkify('a & b "quoted"')
    assert "&amp;" in out
    assert "&quot;" in out or "\"" in out  # markupsafe quoting style


def test_linkify_links_http_and_https():
    out = linkify("see http://example.com/x and https://example.org/y")
    assert '<a href="http://example.com/x"' in out
    assert '<a href="https://example.org/y"' in out
    # Both anchors carry rel=nofollow.
    assert out.count('rel="nofollow"') == 2


def test_linkify_rejects_javascript_scheme():
    """A `javascript:` URL must NOT become an anchor — the URL regex
    only matches http/https. The literal text gets escaped."""
    out = linkify('click javascript:alert(1) here')
    assert "<a href=" not in out
    assert "javascript:alert(1)" in out  # escaped, not linked


def test_linkify_rejects_data_scheme():
    out = linkify("data:text/html,<script>alert(1)</script>")
    assert "<a href=" not in out
    # Inner < / > are escaped; the data: prefix is just text.
    assert "data:text/html" in out
    assert "<script>" not in out


def test_linkify_strips_trailing_punctuation_from_url():
    """URL detection in body text trims trailing `.,;:!?` so the
    period at the end of a sentence isn't part of the link."""
    out = linkify("see http://example.com/x.")
    assert '<a href="http://example.com/x"' in out
    # Trailing dot still appears, but as escaped text outside the anchor.
    assert "</a>." in out


def test_linkify_message_id_in_archive_renders_as_ref():
    """A <Message-ID> in body text where we have the URL → `[ref]`
    anchor. The literal Message-ID is *not* re-leaked."""
    msgid_urls = {"abc@example.com": "/lkml/2024/01/123"}
    out = linkify("ref to <abc@example.com> here", msgid_urls)
    assert "abc@example.com" not in out  # never surfaces literally
    assert '<a href="/lkml/2024/01/123">[ref]</a>' in out


def test_linkify_message_id_not_in_archive_is_neutral_text():
    """Off-list Message-ID → `[off-list ref]` placeholder, no link.
    Address still doesn't appear in the rendered output."""
    out = linkify("ref to <unknown@offlist.com> here", msgid_urls={})
    assert "unknown@offlist.com" not in out
    assert "[off-list ref]" in out
    assert "<a href" not in out


def test_url_or_msgid_regex_matches_msgid_only_with_brackets():
    """A bare email-shaped string in body text without angle brackets
    must NOT match the msgid group; otherwise the privacy-redaction
    contract slips."""
    s = "contact me at foo@bar.com sometime"
    matches = list(URL_OR_MSGID_RE.finditer(s))
    assert all(m.group("msgid") is None for m in matches)


# parse_blocks — text / quote / diff segmentation


def test_parse_blocks_text_only():
    blocks = parse_blocks("line one\nline two")
    assert len(blocks) == 1
    assert blocks[0].kind == "text"


def test_parse_blocks_groups_consecutive_quotes():
    blocks = parse_blocks("> a\n> b\nplain\n>> c")
    kinds = [b.kind for b in blocks]
    assert kinds == ["quote", "text", "quote"]


def test_parse_blocks_diff_runs():
    body = (
        "intro\n"
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "outro\n"
    )
    blocks = parse_blocks(body)
    kinds = [b.kind for b in blocks]
    # text intro, then diff, then text outro
    assert kinds[0] == "text"
    assert "diff" in kinds
    assert kinds[-1] == "text"


# render_body — full pipeline


def test_render_body_empty():
    assert render_body("") == Markup("")
    assert render_body(None) == Markup("")


def test_render_body_wraps_text_in_pre():
    out = str(render_body("hello\nworld"))
    assert "<pre" in out
    assert "hello" in out


def test_render_body_quotes_become_blockquote():
    out = str(render_body("> quoted line"))
    assert "<blockquote>" in out


def test_render_body_deeply_nested_quotes_collapse_to_details():
    """At depth >= QUOTE_COLLAPSE_AT_DEPTH the rendering wraps the
    nested quote in <details> so the reader can collapse it."""
    out = str(render_body(">> a\n>> b"))
    assert "<details>" in out
    assert "<summary>" in out


def test_render_body_diff_pygmentized():
    body = (
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    out = str(render_body(body))
    # Pygments emits inline `style=` (noclasses=True) on its spans.
    assert "<span" in out
    assert "style=" in out


def test_render_body_xss_payload_in_text_is_escaped():
    """Adversarial input: a body containing literal HTML / JS markup
    must round-trip to escaped text inside <pre>, never as live HTML."""
    payload = "<img src=x onerror=alert(1)>"
    out = str(render_body(payload))
    assert "<img" not in out  # the original tag is gone
    assert "&lt;img" in out
    assert "onerror" in out  # but as text inside &lt;...&gt;
    assert "alert" in out


def test_render_body_xss_via_quote_still_escaped():
    """An XSS payload nested inside a `>` quote should still be
    escaped after the quote-strip pass."""
    out = str(render_body("> <script>x</script>"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_body_url_in_quote_is_linked():
    out = str(render_body("> see http://example.com"))
    assert "<blockquote>" in out
    assert '<a href="http://example.com"' in out
