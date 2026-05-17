"""Diff rendering: per-hunk anchors + per-language Pygments overlay
on context / + / - lines (#211).

`_render_diff_block` is the public entry, called by the orchestrator
in `body.py` for every block returned by `parse_blocks` with
`kind == "diff"`.

Per-hunk anchors (`#h-2`, `#h-2-L15`) make URL-fragment deep-links
to specific lines viable from the message page. Per-language lexer
is sniffed from each `+++ b/<path>` line and stays active until the
next `+++` switch; context / + / - lines get a per-line
`_highlight_inline` against that lexer.
"""
import html
import re

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

from mimir.rendering.blocks import DIFF_TRAILER_LINE

# Inline-span formatter for per-language overlay on diff content
# lines (#211): no surrounding `<pre>` or wrapper div, just the
# token spans. `noclasses=False` so the existing `.highlight .k`,
# `.highlight .nf` etc. CSS rules in `mimir.css` apply.
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
# style as Generic.Heading. Mirrors `DIFF_HEADER_RE` in `blocks.py`
# plus the `diff --git` opener; pin so the manual renderer keeps the
# same heading style on those lines.
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
        # `DIFF_TRAILER_LINE` handling in `blocks.parse_blocks`.)
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
