"""Body → HTML pipeline contract.

Public-facing archive's biggest single regression risk lives in
`mimir.rendering`. Route smoke tests use real lkml data, so
adversarial inputs (XSS payloads, weird-scheme URLs, deeply nested
quotes) never get exercised that way. These tests pin the
escaping behavior and the structural transformations.
"""
import re

from markupsafe import Markup

from mimir.rendering import URL_OR_MSGID_RE, linkify, parse_blocks, render_body


# linkify, escaping + URL handling


def test_linkify_escapes_html_metacharacters():
    out = linkify("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&lt;/script&gt;" in out


def test_linkify_escapes_ampersand_and_quote():
    """markupsafe's quoting style is deterministic -- pin the
    `&quot;` form directly. The old `&quot; or "` disjunction would
    have masked a regression where escaping stopped applying to the
    `"` character (raw quotes leaking into rendered HTML)."""
    out = linkify('a & b "quoted"')
    assert "&amp;" in out
    assert "&quot;" in out
    # Negative: the raw form must NOT appear in escaped output.
    assert '"quoted"' not in out


def test_linkify_links_http_and_https():
    out = linkify("see http://example.com/x and https://example.org/y")
    assert '<a href="http://example.com/x"' in out
    assert '<a href="https://example.org/y"' in out
    # Both anchors carry rel=nofollow.
    assert out.count('rel="nofollow"') == 2


def test_linkify_rejects_javascript_scheme():
    """A `javascript:` URL must NOT become an anchor, the URL regex
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


# parse_blocks, text / quote / diff segmentation


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


# render_body, full pipeline


def test_render_body_empty():
    assert render_body("") == Markup("")
    assert render_body(None) == Markup("")


def test_render_body_wraps_text_in_pre():
    """A plain text block must be enclosed by a <pre> open + close
    around the actual content, not just contain a `<pre` substring
    somewhere on the page (which would also pass if the body
    rendered the text raw and a *different* block, diff, blockquote
   , happened to be a `<pre>` further down)."""
    out = str(render_body("hello\nworld"))
    m = re.search(r"<pre[^>]*>(.*?)</pre>", out, re.DOTALL)
    assert m is not None, f"no <pre>...</pre> in output: {out!r}"
    assert "hello" in m.group(1)
    assert "world" in m.group(1)


def test_render_body_quotes_become_blockquote():
    """A `> ` line must be wrapped in a real <blockquote>...</blockquote>
    pair containing the quoted text, not just any open tag."""
    out = str(render_body("> quoted line"))
    m = re.search(r"<blockquote[^>]*>(.*?)</blockquote>", out, re.DOTALL)
    assert m is not None, f"no <blockquote>...</blockquote>: {out!r}"
    assert "quoted line" in m.group(1)


def test_render_body_deeply_nested_quotes_collapse_to_details():
    """At depth >= QUOTE_COLLAPSE_AT_DEPTH the rendering wraps the
    nested quote in <details> so the reader can collapse it."""
    out = str(render_body(">> a\n>> b"))
    assert "<details>" in out
    assert "<summary>" in out


def test_render_body_first_level_quoted_diff_folds_as_hunk_quote():
    """A reviewer's first-level quote of a patch hunk gets folded
    into a `<details class="hunk-quote">` so a wall of quoted diff
    doesn't bury the inline commentary. Without the fold, a long
    review thread is unreadable."""
    body = (
        "Some context above.\n\n"
        "> @@ -1,3 +1,3 @@\n"
        ">  int main(void) {\n"
        "> -    return 0;\n"
        "> +    return 1;\n"
        ">  }\n\n"
        "And my comment below."
    )
    out = str(render_body(body))
    assert '<details class="hunk-quote">' in out
    assert "quoted hunk" in out
    # The diff inside renders via Pygments, its tokens flow into
    # the <blockquote>. Pin one token class to confirm the diff
    # path ran (not just a text dump).
    assert 'class="highlight"' in out


def test_render_body_first_level_quoted_diff_emits_jump_to_hunk_with_parent_url():
    """When the renderer is given `parent_url`, the hunk-quote
    `<summary>` carries a clickable "↗ jump to hunk" link
    pointing at it. The link is HTML-escaped so a hostile parent
    URL can't smuggle markup through."""
    body = "> @@ -1 +1 @@\n> -x\n> +y\n"
    out = str(render_body(body, parent_url="/lkml/2024/01/42"))
    assert "↗ jump to hunk" in out
    assert 'href="/lkml/2024/01/42"' in out


def test_render_body_first_level_quoted_diff_without_parent_url_omits_link():
    """No parent URL → the fold still happens (the wall-of-diff
    is the primary concern), but no link is emitted."""
    body = "> @@ -1 +1 @@\n> -x\n> +y\n"
    out = str(render_body(body))
    assert '<details class="hunk-quote">' in out
    assert "↗ jump to hunk" not in out


def test_render_body_first_level_quoted_text_only_stays_as_blockquote():
    """A first-level quote without a diff inside stays as a plain
    `<blockquote>`, only hunk-quotes get the fold. The immediate
    context of a reply must remain visible by default."""
    out = str(render_body("> just text, no diff here"))
    assert '<details class="hunk-quote">' not in out
    assert "<blockquote>" in out


def test_render_body_hunk_quote_jump_link_escapes_parent_url():
    """The parent URL flows from server-built strings, but defense
    in depth: characters that could break out of the `href`
    attribute must be HTML-escaped."""
    body = "> @@ -1 +1 @@\n> -a\n> +b\n"
    out = str(render_body(body, parent_url='/x"><script>alert(1)</script>'))
    # The hostile chars must NOT appear unescaped inside the rendered
    # output (the renderer html-escapes them on the way into href).
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out or "&quot;" in out


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


def test_render_body_fenced_code_block_default_c():
    """A bare triple-backtick fence defaults to the C lexer, the
    kernel-list context is overwhelmingly C-shaped, and an
    unspecified fence is most commonly someone pasting a code
    snippet in C. Pin: class-based output (Pygments token classes
    present, no inline styles)."""
    body = (
        "Here's a fix:\n\n"
        "```\n"
        "static int foo(void) {\n"
        "    return -EINVAL;\n"
        "}\n"
        "```\n\n"
        "What do you think?\n"
    )
    out = str(render_body(body))
    assert 'class="highlight"' in out
    # `static`, `int`, `return` are C keywords → Pygments emits
    # them with the `k` (keyword) token class.
    assert 'class="k">static</span>' in out
    assert 'class="k">return</span>' in out
    # Surrounding prose still renders as `<pre>` text (not part of
    # the highlighted block).
    assert "Here&#x27;s a fix" in out or "Here's a fix" in out
    assert "What do you think?" in out
    # No inline colours; the page-level CSS handles theming.
    assert 'style="color' not in out


def test_render_body_fenced_code_block_with_python_info_string():
    """`\\`\\`\\`python` selects the Python lexer. Pygments' Python
    token class for `def` is `k` (keyword)."""
    body = (
        "```python\n"
        "def hello():\n"
        "    return 42\n"
        "```\n"
    )
    out = str(render_body(body))
    assert 'class="highlight"' in out
    # `def` is a Python keyword, same `k` class. Distinguish from C
    # by checking for `class="nf">hello`: Pygments tags function
    # definitions in Python with the `nf` (Name.Function) class
    # which C doesn't use the same way.
    assert 'class="nf">hello</span>' in out


def test_render_body_fenced_code_block_unknown_language_falls_back_to_text():
    """An unknown info string (e.g. `\\`\\`\\`foobar`) falls back to
    `TextLexer` rather than raising. The block still renders as
    monospace; just no token classes."""
    body = (
        "```foobar-not-a-language\n"
        "literal text not highlighted\n"
        "```\n"
    )
    out = str(render_body(body))
    assert 'class="highlight"' in out
    assert "literal text not highlighted" in out


def test_render_body_fenced_code_block_excludes_delimiters_from_output():
    """The triple-backtick fence delimiters are markdown wrappers,
    not code. They must not appear in the rendered block."""
    body = (
        "```c\n"
        "int x = 1;\n"
        "```\n"
    )
    out = str(render_body(body))
    # The opening / closing ``` lines aren't part of the C block.
    assert "```" not in out


def test_render_body_unclosed_fence_runs_to_end_of_body():
    """An unclosed fence (no terminating triple-backtick) runs to
    the end of the body, the same forgiving behaviour markdown
    renderers use. Better than dropping the content silently.
    Pygments tokenises each word, so we check the tokens land in
    the highlight block in order."""
    body = (
        "```c\n"
        "int x = 1;\n"
        "still inside the fence\n"
    )
    out = str(render_body(body))
    assert 'class="highlight"' in out
    # The text is tokenised by Pygments into per-word `<span>`s;
    # confirm each token lands in source order inside the
    # highlighted block (no prose pipeline rendering kicked in).
    for token in ("still", "inside", "the", "fence"):
        assert f">{token}</span>" in out


def test_render_body_text_after_closed_fence_is_plain_pre():
    """Re-entering prose after a closed fence must use the plain
    `<pre>` text pipeline, not stay in code mode. Pins the
    state-machine transition out of the fence."""
    body = (
        "```c\n"
        "int x;\n"
        "```\n"
        "back to prose with https://example.test\n"
    )
    out = str(render_body(body))
    # The post-fence URL should be linkified, that only happens in
    # the prose pipeline, not in a code block.
    assert 'href="https://example.test"' in out


def test_render_body_fence_inside_quote_is_treated_as_quote_content():
    """A reviewer quoting code (`> \\`\\`\\`c`) keeps the quote
    structure; we don't re-enter code mode inside a blockquote.
    The quote depth check runs first."""
    body = (
        "> ```c\n"
        "> int x = 1;\n"
        "> ```\n"
        "Followup prose.\n"
    )
    out = str(render_body(body))
    assert "<blockquote>" in out
    # The fence opener inside the quote shouldn't have toggled the
    # state machine for the post-quote prose.
    assert "Followup prose." in out


def test_parse_blocks_records_code_fence_info_string():
    """The block dataclass carries the info-string forwards so the
    renderer can pick the right lexer per block, not just at the
    parse-blocks level."""
    blocks = parse_blocks("```python\nprint(1)\n```\n")
    code_blocks = [b for b in blocks if b.kind == "code"]
    assert len(code_blocks) == 1
    assert code_blocks[0].info == "python"
    assert code_blocks[0].lines == ["print(1)"]


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
    # Exactly one Pygments wrapper, chopping into multiple blocks
    # was the regression that broke copy-paste-to-patch.
    assert out.count('class="highlight"') == 1
    # Every patch component is inside that single block, in source
    # order: header → index → ---/+++ → hunk → -/+ lines → trailer.
    # Add/remove lines render as a marker span + per-language
    # content span (#211), so the `-old`/`+new` concatenation no
    # longer exists in the raw HTML; check for the marker span
    # itself, which is the new stable substring.
    idx_diff = out.index("diff --git")
    idx_index = out.index("index abc1234..def5678")
    idx_minus_file = out.index("--- a/x")
    idx_plus_file = out.index("+++ b/x")
    idx_hunk = out.index("@@ -1 +1 @@")
    idx_old = out.index("old", out.index('class="gd">-</span>'))
    idx_new = out.index("new", out.index('class="gi">+</span>'))
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
    `[off-list ref]` (the msgid lookup failure), a confusing
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
        # email_allowlist tokens by design; that's an intentional
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


def test_trailer_redactor_output_html_metacharacters_are_escaped():
    """The redactor returns plain text; the renderer must escape it
    before splicing into HTML output. A redactor that returned literal
    angle brackets and metachars (which the production redactor does:
    `<{email}>` for allowlisted senders) would otherwise smuggle live
    HTML into the page, the very XSS surface the redaction is meant
    to close. Pin the contract: whatever the redactor returns, the
    rendered output must not contain unescaped angle brackets coming
    from that return value."""
    # Build a body line that the trailer-recognition regex will match.
    # The redactor returns plain-text content carrying `<`, `>`, and `"`
    #, exactly what an attacker would smuggle through an address with
    # HTML metacharacters in the local-part, given the existing email
    # regex permits them.
    payload = '<a"onmouseover=alert(1)@kernel.org>'
    out = str(render_body(
        "Signed-off-by: X <something@example.com>",
        address_redactor=lambda _e: payload,
    ))
    # The literal angle bracket from the redactor must not introduce
    # a live tag. Escaped form must be present, raw form must not.
    assert payload not in out
    assert "&lt;a&quot;onmouseover=alert(1)@kernel.org&gt;" in out
    # Walk every live tag and assert none carries the smuggled handler.
    _no_live_tag_carries(out, "onmouseover")


def test_trailer_email_regex_excludes_html_metacharacters():
    """Defense in depth on top of the renderer escaping: the email
    regex itself shouldn't capture local-parts that contain HTML
    metacharacters (`"`, `<`, `'`, `=`). A trailer line carrying such
    an address should fall through to the default `_render_line` path,
    where it gets `html.escape`d as plain text."""
    body = 'Signed-off-by: A <a"b@kernel.org>'
    # With a redactor that would emit unsafe HTML, the regex tightening
    # should mean the redactor isn't called at all for this address.
    seen_emails: list[str] = []

    def spy(email: str) -> str:
        seen_emails.append(email)
        return f"<{email}>"

    out = str(render_body(body, address_redactor=spy))
    assert seen_emails == [], (
        f"redactor should not have been called for an address containing "
        f"HTML metacharacters; got {seen_emails!r}"
    )
    # The raw payload must be present only as escaped text.
    assert '<a"b@kernel.org>' not in out
    assert "&lt;a&quot;b@kernel.org&gt;" in out


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


# #211: hunk anchors + per-language overlay on context.


def test_render_body_diff_signature_delimiter_not_rendered_as_minus_line():
    """`git format-patch` ends every patch with `-- \\n<version>`.
    The literal `-- ` line is the email signature delimiter, not a
    deletion. Without an explicit guard it lands in the in-hunk
    `" +-"` branch and gets rendered as `<span class="gd">-</span>`
    (a red minus) followed by the per-language overlay on ` `.
    Pin: the trailer area must NOT contain a `gd` marker span; the
    `-- ` line renders as plain text in the diff-meta tail."""
    body = (
        "diff --git a/x.c b/x.c\n"
        "--- a/x.c\n"
        "+++ b/x.c\n"
        "@@ -1 +1 @@\n"
        "-old line\n"
        "+new line\n"
        "-- \n"
        "2.45.0\n"
    )
    out = str(render_body(body))
    # Trailer must render as plain text inside a diff-meta block,
    # NOT inside the hunk as a fake deletion line.
    assert "<pre class=\"diff-meta\">-- \n2.45.0</pre>" in out, (
        "signature delimiter + git version must land in a tail "
        f"diff-meta block, not inside a hunk:\n{out}"
    )
    # gd-styled spans should be: one for `--- a/x.c` (the source-
    # file marker) + one for the real `-old line` deletion. With the
    # bug, the `-- ` trailer line added a third bogus `gd` span.
    assert out.count('class="gd"') == 2, (
        "expected exactly two `gd`-styled spans (source-file marker "
        f"+ one deletion); the trailer line must not add a third:\n{out}"
    )


def test_render_body_diff_hunk_lines_separated_by_newlines():
    """Each line span inside a hunk must be separated by a `\\n` so
    the surrounding `<pre>` renders them on distinct visual lines.
    Without the join, all spans concatenate and the patch collapses
    into a single horizontal stream (visual bug observed pre-fix on
    `dev-patch/2024/06/65`)."""
    body = (
        "diff --git a/x.c b/x.c\n"
        "--- a/x.c\n"
        "+++ b/x.c\n"
        "@@ -1,3 +1,3 @@\n"
        " line one\n"
        "-line two\n"
        "+line two changed\n"
    )
    out = str(render_body(body))
    # Inside the hunk's <pre>, line spans must be `\n`-separated.
    # Extract the slice between `<pre>` and `</pre>` of the hunk.
    hunk_pre = out[out.index('class="hunk"'):out.index("</pre></div>")]
    pre_inner = hunk_pre[hunk_pre.index("<pre>") + len("<pre>"):]
    # At least three newlines between line spans (@@ + 3 content lines = 4 lines).
    assert pre_inner.count("\n") >= 3, (
        f"hunk body lacks newline separators between line spans:\n{pre_inner}"
    )


def test_render_body_diff_hunk_anchor_per_hunk_and_per_line():
    """Each hunk renders inside `<div id="h-N" class="hunk">` and
    every line within the hunk carries `id="h-N-LM"` (1-indexed,
    including the `@@` header itself as line 1). Pins the anchor
    scheme so URLs like `#h-2-L15` jump to line 15 of hunk 2."""
    body = (
        "diff --git a/x.txt b/x.txt\n"
        "--- a/x.txt\n"
        "+++ b/x.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " context line\n"
        "-old\n"
        "+new\n"
        "@@ -10,1 +10,1 @@\n"
        "-foo\n"
        "+bar\n"
    )
    out = str(render_body(body))
    # Two hunks → two hunk-level anchors, sequential numbering.
    assert 'id="h-1" class="hunk"' in out
    assert 'id="h-2" class="hunk"' in out
    # Hunk 1: @@ header is L1, context is L2, -old is L3, +new is L4.
    assert 'id="h-1-L1"' in out
    assert 'id="h-1-L2"' in out
    assert 'id="h-1-L3"' in out
    assert 'id="h-1-L4"' in out
    # Hunk 2 restarts the line counter from 1.
    assert 'id="h-2-L1"' in out
    assert 'id="h-2-L2"' in out
    assert 'id="h-2-L3"' in out


def test_render_body_diff_hunk_anchors_unique_in_output():
    """Anchor IDs must be unique across the rendered HTML; a
    duplicate `id` would silently break URL-fragment jumps to the
    second occurrence. Pin by counting each generated anchor."""
    import re as _re
    body = (
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "@@ -10 +10 @@\n"
        "-c\n"
        "+d\n"
    )
    out = str(render_body(body))
    ids = _re.findall(r'id="(h-\d+(?:-L\d+)?)"', out)
    assert len(ids) == len(set(ids)), (
        f"duplicate anchor IDs in rendered diff: {ids}"
    )


def test_render_body_diff_context_line_gets_per_language_highlight():
    """A C patch's context lines (` ` prefix, no add/remove) carry
    per-language Pygments spans, picked up from the `+++ b/<path>`
    target. `if` is a C keyword and renders as `class="k">if</span>`,
    same token class the fenced-code-block tests check for."""
    body = (
        "diff --git a/foo.c b/foo.c\n"
        "--- a/foo.c\n"
        "+++ b/foo.c\n"
        "@@ -1,3 +1,3 @@\n"
        " if (x > 0) {\n"
        "-    return -EINVAL;\n"
        "+    return 0;\n"
        "}\n"
    )
    out = str(render_body(body))
    # C keyword `if` in the context line picks up the `k` class.
    assert 'class="k">if</span>' in out
    # `return` on add/remove lines also gets per-language coloring
    # (the marker stays in its DiffLexer prefix span, the content
    # gets the per-language overlay).
    assert 'class="k">return</span>' in out


def test_render_body_diff_target_path_drives_lexer_choice():
    """Two-file patch: `.py` target should pick the Python lexer
    (Pygments tags Python function defs as `nf`, not just `n`).
    Verifies the `+++` switch flips the active lexer mid-block."""
    body = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n"
        "-def old(): pass\n"
        "+def new(): pass\n"
    )
    out = str(render_body(body))
    # Python's lexer tags `def` as `k` and the function name as `nf`.
    # The C lexer does NOT tag identifiers as `nf` in the same shape;
    # `nf` is the Python-specific discriminator.
    assert 'class="nf">new</span>' in out
    assert 'class="nf">old</span>' in out


def test_render_body_diff_unknown_extension_falls_back_to_text():
    """A target path Pygments can't recognise (project-internal
    extension, no extension, weird suffix) falls back to `TextLexer`,
    no per-language spans, no crash on `ClassNotFound`."""
    body = (
        "diff --git a/CONFIG b/CONFIG\n"
        "--- a/CONFIG\n"
        "+++ b/CONFIG\n"
        "@@ -1 +1 @@\n"
        " just plaintext\n"
        "-old setting\n"
        "+new setting\n"
    )
    out = str(render_body(body))
    # No crash, hunk anchors still present.
    assert 'id="h-1"' in out
    # No language-keyword spans on the content (TextLexer emits no
    # token classes beyond the default).
    assert 'class="k">' not in out


def test_render_body_diff_binary_patch_renders_without_crash():
    """A `Binary files X and Y differ` line lands in the metadata
    section as a heading-styled span; no hunks follow, no crash."""
    body = (
        "diff --git a/img.png b/img.png\n"
        "index 1..2 100644\n"
        "Binary files a/img.png and b/img.png differ\n"
    )
    out = str(render_body(body))
    assert 'class="highlight"' in out
    assert "Binary files" in out
    # No hunk → no anchors.
    assert 'class="hunk"' not in out


def test_render_body_diff_inside_quote_skips_anchors():
    """Quoted-hunk excerpts inside a reply must NOT emit anchor IDs:
    they'd collide with the primary patch's `h-1` / `h-1-L2` on
    pages where both render. Per-language overlay still applies
    (no anchor != no syntax highlighting); only the `id` attribute
    is suppressed."""
    body = (
        "> diff --git a/foo.c b/foo.c\n"
        "> --- a/foo.c\n"
        "> +++ b/foo.c\n"
        "> @@ -1 +1 @@\n"
        "> -old\n"
        "> +new\n"
    )
    out = str(render_body(body))
    # Quoted-hunk fold uses <details class="hunk-quote">.
    assert 'class="hunk-quote"' in out
    # No anchor IDs in the rendered HTML.
    assert 'id="h-1"' not in out
    assert 'id="h-1-L1"' not in out
    # The DiffLexer-style hunk-heading class is still there.
    assert 'class="gu"' in out


def test_render_body_diff_dev_null_target_path_falls_back_to_text():
    """File-deletion patches use `+++ /dev/null`. Strip-down lexer
    lookup must NOT pass `/dev/null` to `get_lexer_for_filename`
    (which would crash or pick an unrelated lexer); falls back to
    `TextLexer`."""
    body = (
        "diff --git a/gone.c b/gone.c\n"
        "deleted file mode 100644\n"
        "--- a/gone.c\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-int main(void) {}\n"
    )
    out = str(render_body(body))
    # No crash, hunk anchor present.
    assert 'id="h-1"' in out
    # Per-language overlay applies to `--- a/gone.c` would be the
    # SOURCE side, but our lexer choice keys on `+++` (the target).
    # `/dev/null` → TextLexer → no `k` keyword spans.
    assert 'class="k">' not in out
