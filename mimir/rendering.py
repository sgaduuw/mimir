import html
import re
from dataclasses import dataclass, field

from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import DiffLexer, get_lexer_by_name, get_lexer_for_filename
from pygments.lexers.c_cpp import CLexer
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

QUOTE_PREFIX_RE = re.compile(r"^((?:>\s?)+)")
STRIP_ONE_LEVEL_RE = re.compile(r"^>\s?")
DIFF_START_RE = re.compile(r"^(?:diff --git |--- \S|\+\+\+ \S|@@ )")
# Non-body diff metadata lines that should stay inside a diff block once
# we've entered one. Without this, `index abc..def` (first char `i`)
# breaks the block in half and dumps `diff --git` into its own
# Pygments output while the actual hunk lands in a second one. The
# Pygments DiffLexer recognises every key in this set as
# Generic.Heading, so they get styled correctly inside one block.
DIFF_HEADER_RE = re.compile(
    r"^(?:"
    r"index [0-9a-fA-F]+\.\.[0-9a-fA-F]+"
    r"|new file mode "
    r"|deleted file mode "
    r"|old mode "
    r"|new mode "
    r"|copy from "
    r"|copy to "
    r"|rename from "
    r"|rename to "
    r"|similarity index "
    r"|dissimilarity index "
    r"|Binary files .+ differ$"
    r")"
)
# `git format-patch` ends every patch with `-- \n<version>\n`. The
# "-- " line (two dashes + trailing space) is the email signature
# delimiter, and everything after it (the git version, typically one
# line) is part of the canonical patch payload, copy-paste-to-patch
# expects it. Once we see this line inside a diff block, swallow the
# rest of the message into the same block.
DIFF_TRAILER_LINE = "-- "
URL_OR_MSGID_RE = re.compile(
    r'(?P<url>https?://[^\s<>"\'\]\)]+)'
    r"|<(?P<msgid>[A-Za-z0-9._%+\-=$/]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>"
)
# DCO trailers are process attestations, not contact info. We still
# redact addresses that aren't allowlisted, but we don't want the
# msgid linkifier to mistake the address for a missing message-ID
# and emit `[off-list ref]`, that actively looks like broken
# metadata and breaks DCO chain verification on display.
TRAILER_KEYS = (
    "Signed-off-by",
    "Reviewed-by",
    "Tested-by",
    "Acked-by",
    "Co-developed-by",
    "Reported-by",
    "Suggested-by",
    "Reported-and-tested-by",
    "Cc",
    "To",
    "From",
)
_TRAILER_LINE_RE = re.compile(
    r"^(?:" + "|".join(re.escape(k) for k in TRAILER_KEYS) + r"):\s",
    re.IGNORECASE,
)
# Local-part / domain restricted to a conservative RFC 5322 subset that
# excludes HTML metacharacters (`"`, `'`, `<`, `=`, etc.). The trailer
# renderer also html-escapes the redactor's return value as defense in
# depth; tightening here means hostile addresses fall through to the
# default `_render_line` path (which always escapes) instead of going
# through the redactor branch at all.
_EMAIL_ANGLE_RE = re.compile(r"<([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+)>")

_DIFF_LEXER = DiffLexer()
# Class-based output so the diff inherits the page theme via CSS.
# Inline styles never adapt to dark mode; classes pair with the CSS
# block in base.html that uses Pico's variables + light-dark() so
# add/remove colours stay legible in both modes.
_DIFF_FORMATTER = HtmlFormatter(noclasses=False, nobackground=True, cssclass="highlight")
# Inline-span formatter for per-language overlay on diff content
# lines (#211): no surrounding `<pre>` or wrapper div, just the
# token spans. Same `noclasses=False` so the existing
# `.highlight .k`, `.highlight .nf` etc. CSS rules apply.
_INLINE_LANG_FORMATTER = HtmlFormatter(nowrap=True, noclasses=False)


# Hunk header is the line that opens a hunk inside a diff. We split
# diff blocks on this marker so each hunk gets its own anchor + a
# bounded slice for per-language re-lexing.
_HUNK_HEADER_RE = re.compile(r"^@@ ")
# Target-path extractor on the `+++ b/<path>` line. Group 1 is the
# path, used by `_lexer_for_path` to pick a per-file language lexer.
# `+++ /dev/null` lands in group 1 as `/dev/null` and falls through
# to the TextLexer branch.
_DIFF_PLUS_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?(\S+)")
# Non-`+++`/`---` pre-hunk metadata that Pygments' DiffLexer would
# style as Generic.Heading. Mirrors `DIFF_HEADER_RE` above plus the
# `diff --git` opener; pin so the manual renderer keeps the same
# heading style on those lines.
_DIFF_META_HEADING_RE = re.compile(
    r"^(?:"
    r"diff --git "
    r"|index [0-9a-fA-F]+\.\.[0-9a-fA-F]+"
    r"|new file mode "
    r"|deleted file mode "
    r"|old mode "
    r"|new mode "
    r"|copy from "
    r"|copy to "
    r"|rename from "
    r"|rename to "
    r"|similarity index "
    r"|dissimilarity index "
    r"|Binary files .+ differ$"
    r")"
)

# Fenced-code-block detection. Markdown-style triple-backtick fence
# with an optional info string identifying the language:
#   ```          , opens a fence; language defaults to C (kernel
#                   list discussions are overwhelmingly C-shaped)
#   ```c         , opens a C fence
#   ```python    , opens a Python fence
#   ```          , closes whichever fence is open
#
# Why fences only (not indent-based detection): high precision.
# Markdown's 4-space-indent code blocks would false-positive on any
# mail client that quotes with leading whitespace; the fence
# discriminator is unambiguous and stays inside its delimiters.
_FENCE_RE = re.compile(r"^\s*```\s*([A-Za-z0-9_+-]*)\s*$")

_CODE_FORMATTER = HtmlFormatter(noclasses=False, nobackground=True, cssclass="highlight")

_DEFAULT_CODE_LEXER = CLexer()


def _lexer_for_fence(info: str):
    """Pick a Pygments lexer for a code fence's info string.

    Empty info → `CLexer` (kernel-list discussions default to C).
    Known language → that lexer.
    Unknown name → fall back to `TextLexer` so the block renders as
    monospace plaintext rather than crashing on `ClassNotFound`.
    """
    if not info:
        return _DEFAULT_CODE_LEXER
    try:
        return get_lexer_by_name(info)
    except ClassNotFound:
        return TextLexer()


def _lexer_for_diff_target(path: str | None):
    """Pick a Pygments lexer for the target path of a diff hunk
    (`+++ b/<path>`). `None`, `/dev/null` (file deletion), and
    extensions Pygments doesn't recognise (binary patches,
    project-internal extensions) all collapse to `TextLexer` so the
    content renders as monospace without per-language coloring."""
    if not path or path == "/dev/null":
        return TextLexer()
    try:
        return get_lexer_for_filename(path)
    except ClassNotFound:
        return TextLexer()


def _highlight_inline(text: str, lexer) -> str:
    """Lex `text` through `lexer` and return inline-span HTML, no
    `<pre>` or wrapper div. Trailing newlines stripped, Pygments
    appends one in `nowrap` mode and we splice the result inside a
    line-scoped `<span>`. Empty input short-circuits because
    Pygments otherwise emits a stray newline that breaks the
    enclosing `<pre>`'s line accounting."""
    if not text:
        return ""
    return highlight(text, lexer, _INLINE_LANG_FORMATTER).rstrip("\n")


def _render_diff_block(lines: list[str], with_anchors: bool = True) -> str:
    """Render a diff block with per-hunk anchors + per-language
    overlay (#211).

    `with_anchors=False` skips emitting `id="..."` attributes; used
    when the diff is nested inside a quote block (the parent patch's
    `h-1` etc. anchors would otherwise collide with the quoted
    excerpt's). Deep-linking into a quoted excerpt isn't a real
    workflow; deep links go to the original patch's anchors.

    Output shape:
        <div class="highlight">
          <pre class="diff-meta">  -- pre-hunk metadata: diff --git,
                                      index, ---, +++, file-mode,
                                      similarity-index, etc.
            <span class="gh">diff --git a/x b/x</span>
            <span class="gh">index abc..def 100644</span>
            <span class="gd">--- a/x</span>
            <span class="gi">+++ b/x</span>
          </pre>
          <div id="h-1" class="hunk"><pre>  -- one hunk per div
            <span id="h-1-L1" class="gu">@@ -1,3 +1,3 @@</span>
            <span id="h-1-L2"> <em>context with per-lang highlight</em></span>
            <span id="h-1-L3"><span class="gd">-</span>old</span>
            <span id="h-1-L4"><span class="gi">+</span>new</span>
          </pre></div>
          <div id="h-2" class="hunk">...</div>
        </div>

    Hunk anchors (`#h-2`) and per-line anchors (`#h-2-L15`) make
    URL-fragment deep-links to specific lines of a patch viable.
    Per-language lexer is detected from each `+++ b/<path>` line
    and stays active until the next `+++` switch; context / + / -
    lines get a per-line Pygments call against that lexer.
    """
    out: list[str] = []
    meta_buffer: list[str] = []
    hunk_buffer: list[str] = []
    current_lexer = TextLexer()
    in_hunk = False
    in_trailer = False
    hunk_idx = 0
    line_in_hunk = 0

    def flush_meta() -> None:
        if meta_buffer:
            out.append(
                '<pre class="diff-meta">' + "\n".join(meta_buffer) + "</pre>"
            )
            meta_buffer.clear()

    def close_hunk() -> None:
        nonlocal in_hunk
        if in_hunk:
            # Per-line spans are joined with `\n` inside the <pre>; the
            # browser turns those into visible line breaks. Without
            # this, all line spans collapse onto a single line.
            out.append(
                f'<div{hunk_attr()} class="hunk"><pre>'
                + "\n".join(hunk_buffer)
                + "</pre></div>"
            )
            hunk_buffer.clear()
            in_hunk = False

    def hunk_attr() -> str:
        return f' id="h-{hunk_idx}"' if with_anchors else ""

    def line_attr() -> str:
        return f' id="h-{hunk_idx}-L{line_in_hunk}"' if with_anchors else ""

    for line in lines:
        # `git format-patch` ends every patch with `-- \n<version>`.
        # The exact `-- ` line is the email signature delimiter and
        # everything after it is git's version, not patch content.
        # Without an explicit check, `-- ` falls into the in-hunk
        # `" +-"` branch below and renders as a stray `<span class
        # ="gd">-</span>` red minus on the message page. Closing
        # the hunk + routing subsequent lines through meta_buffer
        # keeps the trailer rendered as plain text. (Matches the
        # `DIFF_TRAILER_LINE` handling in `parse_blocks`.)
        if line == DIFF_TRAILER_LINE:
            close_hunk()
            in_trailer = True
            meta_buffer.append(html.escape(line))
            continue
        if in_trailer:
            meta_buffer.append(html.escape(line))
            continue

        if _HUNK_HEADER_RE.match(line):
            close_hunk()
            flush_meta()
            hunk_idx += 1
            line_in_hunk = 1
            in_hunk = True
            # @@ line is line 1 of the hunk; carries the hunk-line
            # anchor and the DiffLexer-style hunk-heading colour.
            hunk_buffer.append(
                f'<span{line_attr()} class="gu">{html.escape(line)}</span>'
            )
            continue

        if in_hunk and line and line[0] in " +-":
            line_in_hunk += 1
            marker = line[0]
            content = line[1:]
            highlighted = _highlight_inline(content, current_lexer)
            if marker == "+":
                marker_html = '<span class="gi">+</span>'
            elif marker == "-":
                marker_html = '<span class="gd">-</span>'
            else:
                marker_html = " "
            hunk_buffer.append(
                f'<span{line_attr()}>{marker_html}{highlighted}</span>'
            )
            continue

        if in_hunk and not line:
            # Blank line inside a hunk: rare in well-formed diffs
            # but possible at hunk boundaries. Preserve it as an
            # anchorable empty line so the line-number scheme stays
            # consistent.
            line_in_hunk += 1
            hunk_buffer.append(f'<span{line_attr()}></span>')
            continue

        # Out-of-hunk line. A new `diff --git` (multi-file patch) or
        # tail content after the last hunk (`-- \n<version>`). Close
        # any open hunk and route through the metadata buffer.
        close_hunk()

        if line.startswith("--- "):
            meta_buffer.append(f'<span class="gd">{html.escape(line)}</span>')
        elif line.startswith("+++ "):
            meta_buffer.append(f'<span class="gi">{html.escape(line)}</span>')
            m = _DIFF_PLUS_FILE_RE.match(line)
            current_lexer = _lexer_for_diff_target(m.group(1) if m else None)
        elif _DIFF_META_HEADING_RE.match(line):
            meta_buffer.append(f'<span class="gh">{html.escape(line)}</span>')
        else:
            # Tail content (signature delimiter `-- `, git version,
            # or stray prose). Plain-escape; the DiffLexer treats
            # these the same way (no special class).
            meta_buffer.append(html.escape(line))

    close_hunk()
    flush_meta()

    return '<div class="highlight">' + "".join(out) + "</div>"


@dataclass
class _Block:
    kind: str  # "text" | "quote" | "diff" | "code"
    lines: list[str] = field(default_factory=list)
    # For `code` blocks: the info-string from the fence (`c`,
    # `python`, etc.; empty = default to C). Ignored for other kinds.
    info: str = ""


def _quote_depth(line: str) -> int:
    m = QUOTE_PREFIX_RE.match(line)
    return m.group(1).count(">") if m else 0


def _strip_one_quote_level(line: str) -> str:
    return STRIP_ONE_LEVEL_RE.sub("", line, count=1)


def parse_blocks(text: str) -> list[_Block]:
    """Walk lines, group runs of the same kind into blocks.

    Quote blocks store the *original* prefixed lines; render_block strips
    one level off and recurses, which handles arbitrary nesting naturally.

    Diff blocks aim to capture a full ``git format-patch`` payload as a
    single block: hunk markers, body lines, metadata headers
    (``index ...``, ``new file mode``, ``rename ...``, ``Binary files
    ... differ``), and the trailing ``-- \\n<version>`` signature so
    copy-paste-to-patch keeps the full canonical output. Once the
    trailer line is seen, the remaining lines (typically just the git
    version) are pulled into the same block; this matches what
    ``format-patch`` emits.
    """
    blocks: list[_Block] = []
    in_diff = False
    in_trailer = False
    in_code = False
    code_info = ""

    def push(kind: str, line: str, info: str = "") -> None:
        if blocks and blocks[-1].kind == kind:
            blocks[-1].lines.append(line)
        else:
            blocks.append(_Block(kind=kind, lines=[line], info=info))

    for raw_line in text.splitlines():
        # Code-fence handling takes precedence over everything else
        # once we're inside a fence, quotes, diffs, and prose all
        # stop being meaningful until the closing fence.
        fence = _FENCE_RE.match(raw_line)
        if in_code:
            if fence is not None:
                # Closing fence ends the block; don't include the
                # delimiter line in the rendered code.
                in_code = False
                code_info = ""
            else:
                push("code", raw_line, info=code_info)
            continue
        if fence is not None:
            # Opening fence. The info-string lets the renderer pick
            # a lexer. We deliberately do NOT emit the delimiter as
            # part of the code block, the delimiter is the
            # markdown wrapper, not the code.
            in_code = True
            code_info = fence.group(1).lower()
            in_diff = False  # any prior diff state is irrelevant
            # Seed an empty block so an empty fence still renders
            # something (avoids the "no block emitted" edge case
            # if the operator pasted an empty fence).
            blocks.append(_Block(kind="code", lines=[], info=code_info))
            continue

        if in_diff and in_trailer:
            push("diff", raw_line)
            continue

        if _quote_depth(raw_line) > 0:
            in_diff = False
            push("quote", raw_line)
            continue

        if DIFF_START_RE.match(raw_line):
            in_diff = True
            push("diff", raw_line)
            continue

        if in_diff and DIFF_HEADER_RE.match(raw_line):
            push("diff", raw_line)
            continue

        if in_diff and raw_line and raw_line[0] in " +-":
            push("diff", raw_line)
            if raw_line == DIFF_TRAILER_LINE:
                in_trailer = True
            continue

        if in_diff and not raw_line:
            # blank line inside a diff: keep, hunks have spacing
            push("diff", raw_line)
            continue

        in_diff = False
        push("text", raw_line)

    return blocks


def linkify(
    text: str,
    msgid_urls: dict[str, str] | None = None,
    address_redactor=None,
) -> str:
    """Escape `text` and replace URLs and `<Message-IDs>` with anchor tags.

    Message-IDs are *not* rendered verbatim, they're email-shaped (often
    with @host, sometimes with the literal local-part), and rendering
    them in body text would re-leak the address info we already
    de-leaked from URLs. Instead they collapse to a neutral placeholder:
    `[ref]` (with link, when in archive) or `[off-list ref]` (when not).
    The link still points at the canonical archive URL.

    `address_redactor` is an optional callable ``(email_str) -> str``
    applied to ``<email>`` patterns on DCO trailer lines
    (``Signed-off-by:``, ``Reviewed-by:``, etc.). The callable returns
    *plain text*, typically ``<addr@dom>`` for allowlisted senders
    and ``<redacted>`` otherwise; the renderer html-escapes the
    return value before splicing into output, so the redactor cannot
    smuggle live HTML regardless of what it returns. Without a
    redactor, trailer lines fall through to the msgid linkifier and
    emit ``[off-list ref]`` which actively looks like broken DCO
    metadata.
    """
    msgid_urls = msgid_urls or {}
    rendered_lines: list[str] = []
    for line in text.split("\n"):
        if address_redactor is not None and _TRAILER_LINE_RE.match(line):
            rendered_lines.append(_render_trailer_line(line, address_redactor))
        else:
            rendered_lines.append(_render_line(line, msgid_urls))
    return "\n".join(rendered_lines)


def _render_line(line: str, msgid_urls: dict[str, str]) -> str:
    out: list[str] = []
    pos = 0
    for m in URL_OR_MSGID_RE.finditer(line):
        out.append(html.escape(line[pos : m.start()]))
        if m.group("url"):
            url = m.group("url").rstrip(".,;:!?")
            esc = html.escape(url)
            out.append(f'<a href="{esc}" rel="nofollow">{esc}</a>')
            pos = m.start() + len(m.group("url"))
            trimmed = m.group("url")[len(url):]
            if trimmed:
                out.append(html.escape(trimmed))
        else:
            mid = m.group("msgid")
            href = msgid_urls.get(mid)
            if href:
                out.append(f'<a href="{html.escape(href)}">[ref]</a>')
            else:
                out.append("[off-list ref]")
            pos = m.end()
    out.append(html.escape(line[pos:]))
    return "".join(out)


def _render_trailer_line(line: str, redactor) -> str:
    """Trailer-line variant of `_render_line`: redacts ``<email>``
    patterns via `redactor` instead of msgid-linkifying them. URLs
    elsewhere on the line still get the normal URL treatment.

    The redactor returns *plain text*, including the literal angle
    brackets it wants visible. This renderer html-escapes the return
    value before splicing into output, so a redactor cannot smuggle
    live HTML through this code path regardless of what the email
    regex matched.
    """
    out: list[str] = []
    pos = 0
    spans: list[tuple[int, int, str]] = []
    for m in URL_OR_MSGID_RE.finditer(line):
        if m.group("url"):
            url = m.group("url").rstrip(".,;:!?")
            esc = html.escape(url)
            replacement = f'<a href="{esc}" rel="nofollow">{esc}</a>'
            trimmed = m.group("url")[len(url):]
            if trimmed:
                replacement += html.escape(trimmed)
            spans.append((m.start(), m.end(), replacement))
    for m in _EMAIL_ANGLE_RE.finditer(line):
        spans.append((m.start(), m.end(), html.escape(redactor(m.group(1)))))
    spans.sort()
    for start, end, replacement in spans:
        out.append(html.escape(line[pos:start]))
        out.append(replacement)
        pos = end
    out.append(html.escape(line[pos:]))
    return "".join(out)


def redact_trailer_addresses(text: str, redactor) -> str:
    """Plain-text variant of the DCO trailer redaction the HTML
    renderer applies via `_render_trailer_line`. Walks the body
    line-by-line, applies `redactor` to every `<email>` pattern on
    a `Signed-off-by:` / `Reviewed-by:` / etc. line, and returns
    plaintext with the substitutions in place. Used by surfaces
    that consume body content outside the HTML pipeline (JSON-LD
    `text` snippet today) so the redaction posture stays
    consistent across every place a body byte can flow."""
    out: list[str] = []
    for line in text.split("\n"):
        if _TRAILER_LINE_RE.match(line):
            out.append(_EMAIL_ANGLE_RE.sub(lambda m: redactor(m.group(1)), line))
        else:
            out.append(line)
    return "\n".join(out)


QUOTE_COLLAPSE_AT_DEPTH = 2  # depth at which nested quotes auto-collapse


def _render_block(
    block: _Block,
    msgid_urls: dict[str, str],
    address_redactor=None,
    depth: int = 0,
    parent_url: str | None = None,
) -> str:
    if block.kind == "quote":
        stripped = [_strip_one_quote_level(line) for line in block.lines]
        inner = "\n".join(stripped)
        inner_blocks = parse_blocks(inner)
        inner_html = "".join(
            _render_block(b, msgid_urls, address_redactor, depth + 1, parent_url)
            for b in inner_blocks
        )
        # A first-level quote containing a diff is the patch-review
        # "quoted hunk" pattern: a reviewer pasting a chunk of the
        # parent patch to comment on it. Without folding, deep reviews
        # turn the page into wall-of-diff that buries the inline
        # commentary. Wrap in <details> by default and surface a
        # "↗ jump to hunk" link to the parent message (where the
        # original hunk lives in context).
        is_hunk_quote = (
            depth == 0
            and any(b.kind == "diff" for b in inner_blocks)
        )
        if is_hunk_quote:
            jump = ""
            if parent_url:
                jump = (
                    ' <a href="' + html.escape(parent_url) +
                    '">↗ jump to hunk</a>'
                )
            return (
                '<details class="hunk-quote"><summary>'
                f'<small><em>quoted hunk</em>{jump}</small></summary>'
                f"<blockquote>{inner_html}</blockquote></details>"
            )
        if depth + 1 >= QUOTE_COLLAPSE_AT_DEPTH:
            # Wrap deep quotes in <details> so the user can collapse the
            # nested levels. Pico styles details/summary out of the box.
            return (
                '<details><summary><small><em>quoted</em></small></summary>'
                f"<blockquote>{inner_html}</blockquote></details>"
            )
        return f"<blockquote>{inner_html}</blockquote>"

    if block.kind == "diff":
        # Anchors only at top level; nested (inside a quote block)
        # diffs would collide on `h-N` / `h-N-LM` with the primary
        # patch's anchors on the same page.
        return _render_diff_block(block.lines, with_anchors=(depth == 0))

    if block.kind == "code":
        code_text = "\n".join(block.lines)
        lexer = _lexer_for_fence(block.info)
        return highlight(code_text, lexer, _CODE_FORMATTER)

    text = "\n".join(block.lines)
    return (
        '<pre class="body-text-block">'
        f"{linkify(text, msgid_urls, address_redactor)}</pre>"
    )


def render_body(
    body: str | None,
    msgid_urls: dict[str, str] | None = None,
    address_redactor=None,
    parent_url: str | None = None,
) -> Markup:
    """Render `body` to HTML.

    `parent_url`: if set, first-level quote blocks that contain a
    diff (patch-review "quoted hunk" pattern) are wrapped in a
    `<details>` with a "↗ jump to hunk" link pointing here. Pass
    the URL of the message being replied to (typically resolved
    from `article.thread_parent` via the thread's URL map).
    """
    if not body:
        return Markup("")
    return Markup(
        "".join(
            _render_block(b, msgid_urls or {}, address_redactor,
                          parent_url=parent_url)
            for b in parse_blocks(body)
        )
    )
