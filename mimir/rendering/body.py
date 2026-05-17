"""Top-level body rendering: walks the block list from
`blocks.parse_blocks` and dispatches each block to the right
renderer (diff, linkify, fenced-code) or recurses into a quote.

`render_body` is THE public entry point of the rendering package.
Templates pass it (via the `render_body` Jinja filter wired up in
`mimir/web/filters.py`) the body text + a `msgid_urls` dict for
cross-archive Message-ID linkification + an optional
`address_redactor` for the DCO-trailer pipeline.

The fenced-code-block Pygments work (`_lexer_for_fence`,
`_CODE_FORMATTER`, `_DEFAULT_CODE_LEXER`) lives here next to its
sole caller, `_render_block`'s code-block branch; spinning it into
its own module would create a third tiny rendering submodule for a
~25-line concern that already reads cleanly co-located with the
orchestrator.
"""
import html

from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.lexers.c_cpp import CLexer
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

from mimir.rendering.blocks import _Block, _strip_one_quote_level, parse_blocks
from mimir.rendering.diff import _render_diff_block
from mimir.rendering.linkify import linkify

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
