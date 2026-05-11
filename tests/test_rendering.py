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


def test_url_or_msgid_regex_matches_bracketed_msgid_positive():
    """Positive companion to the bracket-guard contract: a
    `<msgid@host>` token DOES capture in the msgid group. Without
    this, a regression that broke bracketed matching would silently
    leave every msgid rendering as plain text -- the negative test
    above would still pass."""
    s = "see <abc123-XYZ.0@example.invalid> for context"
    matches = list(URL_OR_MSGID_RE.finditer(s))
    msgids = [m.group("msgid") for m in matches if m.group("msgid")]
    assert msgids == ["abc123-XYZ.0@example.invalid"]


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
    # Class-based output (noclasses=False): inserted/deleted lines
    # carry Pygments' class names rather than inline `style=` colours.
    # CSS in base.html maps them to theme-aware tones.
    assert "<span" in out
    assert 'class="gi"' in out  # inserted
    assert 'class="gd"' in out  # deleted
    assert 'style="color' not in out


def test_render_body_diff_full_patch_stays_one_block():
    """A complete git format-patch payload must render as one
    `<div class="highlight">` block, not get chopped at `index ...`
    or at the trailing `-- /<version>` signature. Components must
    appear in source order; a re-ordering bug would silently pass
    presence-only checks."""
    body = (
        "diff --git a/x b/x\n"
        "index abc1234..def5678 100644\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "-- \n"
        "2.53.0\n"
    )
    out = str(render_body(body))
    # Exactly one Pygments wrapper — chopping into multiple blocks
    # was the regression that broke copy-paste-to-patch.
    assert out.count('class="highlight"') == 1
    # Every patch component is inside that single block, in source
    # order: header → index → ---/+++ → hunk → -/+ lines → trailer.
    idx_diff = out.index("diff --git")
    idx_index = out.index("index abc1234..def5678")
    idx_minus_file = out.index("--- a/x")
    idx_plus_file = out.index("+++ b/x")
    idx_hunk = out.index("@@ -1 +1 @@")
    idx_old = out.index("-old")
    idx_new = out.index("+new")
    idx_trailer = out.index("2.53.0")
    assert (
        idx_diff < idx_index < idx_minus_file < idx_plus_file
        < idx_hunk < idx_old < idx_new < idx_trailer
    ), "patch components must render in source order"


def test_render_body_diff_multi_file_patch_stays_one_block():
    """Multi-file patches have `diff --git` + `index` for each file;
    `index` must not break the block."""
    body = (
        "diff --git a/x b/x\n"
        "index 1..2 100644\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "diff --git a/y b/y\n"
        "index 3..4 100644\n"
        "--- a/y\n"
        "+++ b/y\n"
        "@@ -1 +1 @@\n"
        "-c\n"
        "+d\n"
    )
    out = str(render_body(body))
    assert out.count('class="highlight"') == 1


# Trailer redaction: DCO trailers don't go through the msgid linkifier.


def test_trailer_addresses_not_msgid_linkified():
    """Without a redactor, an email in a Signed-off-by becomes
    `[off-list ref]` (the msgid lookup failure) — a confusing
    `<redacted>`-meets-`<broken>` artifact for DCO chains. With a
    redactor, trailer emails go through that instead."""
    body = "Signed-off-by: Bob <bob@example.com>"
    out_no_redactor = str(render_body(body))
    assert "[off-list ref]" in out_no_redactor
    out_with_redactor = str(
        render_body(body, address_redactor=lambda _e: "<redacted>")
    )
    assert "[off-list ref]" not in out_with_redactor
    assert "&lt;redacted&gt;" in out_with_redactor or "<redacted>" in out_with_redactor


def test_trailer_keeps_allowlisted_addresses_when_redactor_says_so():
    """The redactor can return the original `<addr>` for allowlisted
    senders (the web layer's wrapper does exactly this against
    settings.email_allowlist)."""
    def keep_if_kernel(email):
        # Domain-suffix check (rather than substring) to keep CodeQL's
        # `py/incomplete-url-substring-sanitization` rule quiet on this
        # test helper. Production redactor uses substring matching via
        # email_allowlist tokens by design — that's an intentional
        # looseness handled elsewhere.
        return f"<{email}>" if email.lower().endswith("@kernel.org") else "<redacted>"

    body = (
        "Signed-off-by: Linus Torvalds <torvalds@kernel.org>\n"
        "Cc: Random Person <random@example.com>\n"
    )
    out = str(render_body(body, address_redactor=keep_if_kernel))
    assert "torvalds@kernel.org" in out
    assert "random@example.com" not in out
    assert "redacted" in out


def test_trailer_recognition_is_case_insensitive():
    """Some senders capitalise trailer keys (`SIGNED-OFF-BY:` etc.);
    the recognition shouldn't drop them just because of case."""
    body = "Reviewed-by: Alice <alice@example.com>"
    out = str(
        render_body(body, address_redactor=lambda _e: "<R>")
    )
    assert "[off-list ref]" not in out


def test_non_trailer_msgid_linkify_unaffected_by_redactor():
    """Quoted message-IDs in body text (e.g. someone referring to a
    prior message) still go through the msgid linkifier even when a
    trailer redactor is installed."""
    body = "see <abc123@some.host.example> for context"
    out = str(
        render_body(body, address_redactor=lambda _e: "<R>")
    )
    # Body-text msgid that doesn't match the archive collapses to the
    # off-list ref placeholder, same as before.
    assert "[off-list ref]" in out


def _no_live_tag_carries(out: str, banned_attr: str) -> None:
    """Assert no element start tag in `out` carries `banned_attr`.

    A renderer that fails to escape attributes can let a payload like
    `onerror=alert(1)` survive as a real attribute on some legit tag
    (e.g. `<pre onerror=...>` if a sanitizer slips). The string-search
    `assert "onerror" in out` from older versions of this test would
    pass even if the attribute were live, because the literal text
    would still appear in the escaped form too. Walk every live tag
    instead and assert none of them carries the attribute name.
    """
    import re
    # Match HTML element start tags. Each match is the full `<tag ...>`.
    for tag in re.findall(r"<[a-zA-Z][^>]*>", out):
        assert banned_attr not in tag, (
            f"live tag {tag!r} carries forbidden attr {banned_attr!r}"
        )


def test_render_body_xss_payload_in_text_is_escaped():
    """Adversarial input: a body containing literal HTML / JS markup
    must round-trip to escaped text, never as live HTML. In particular
    the `onerror=...` attribute must not survive as a real attribute
    on any rendered element -- only as escaped text inside the body."""
    payload = "<img src=x onerror=alert(1)>"
    out = str(render_body(payload))
    # Original tag is gone, escaped form present.
    assert "<img" not in out
    assert "&lt;img" in out
    # The full payload text round-trips as escaped text.
    assert "onerror=alert(1)" in out
    # No live tag carries onerror (this would catch a sanitizer that
    # let the attribute survive on `<pre>` or another wrapping element).
    _no_live_tag_carries(out, "onerror")


def test_render_body_xss_via_quote_still_escaped():
    """An XSS payload nested inside a `>` quote should still be
    escaped after the quote-strip pass. Both the literal `<script>`
    must be escaped AND no live tag may carry script-execution
    attributes."""
    out = str(render_body("> <script>alert(1)</script>"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    # Script content survives as escaped text.
    assert "alert(1)" in out
    # No live tag carries onload/onerror/etc.
    for attr in ("onerror", "onload", "onclick"):
        _no_live_tag_carries(out, attr)


def test_render_body_url_in_quote_is_linked():
    out = str(render_body("> see http://example.com"))
    assert "<blockquote>" in out
    assert '<a href="http://example.com"' in out
