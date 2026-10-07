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
from collections.abc import Callable
from dataclasses import dataclass

from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.lexers.c_cpp import CLexer
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

from mimir.rendering.blocks import (
    QUOTE_PREFIX_RE,
    _Block,
    _strip_one_quote_level,
    looks_like_orphaned_diff,
    parse_blocks,
)
from mimir.rendering.diff import _render_diff_block
from mimir.rendering.linkify import linkify

_CODE_FORMATTER = HtmlFormatter(
    noclasses=False, nobackground=True, cssclass="highlight"
)
_DEFAULT_CODE_LEXER = CLexer()

# Past this many characters in a single code-fence block we fall
# back to `TextLexer` instead of running the chosen language lexer.
# Pygments has a long history of pathological regex performance on
# crafted inputs for some lexers (CSS, Perl, etc.); a public, no-auth
# message-view route is the easiest place for a bot to pin a worker
# until the gunicorn timeout (60 s) by repeated GETs against a
# crafted body. The 64 KiB threshold is well above any realistic
# inline code block (typical hunks are sub-KB) but small enough that
# even a worst-case TextLexer run on the same content stays in the
# tens of milliseconds. Same threshold reused by diff and attachment
# preview rendering.
PYGMENTS_MAX_BLOCK_CHARS = 64 * 1024

# Deepest quote nesting `_render_block` will descend into. Past it,
# the remaining lines render flat, prefixes and all.
#
# Rendering a quote recurses (strip one `>`, re-parse, render), so
# without a cap the depth is the attacker's to choose and the stack
# is the only limit. There is no single number for it: measured
# 2026-09-29 at depth 497 from a bare call and 481 under pytest,
# because what is left of the stack depends on how many frames the
# caller already used, and a real request has Flask and Jinja on it.
# Whatever the exact figure, blowing it is a 500 on a public
# no-auth route, for as long as the message stays archived, which
# is forever: the mirror is the source of truth and the body is
# re-derived on every read. Same class as the caps above, and the
# one that was missing (#581).
#
# 64 against a deepest-observed 7: measured 2026-09-29 over 1,000
# messages from two inboxes (600 lkml, 400 linux-wireless), where
# nothing exceeded 8. Re-measure before treating that as a fact;
# the archive only grows.
MAX_QUOTE_DEPTH = 64

# Total quote levels one `render_body` call will descend into,
# across every quote block in the message.
#
# `MAX_QUOTE_DEPTH` bounds one block; it does not bound the message,
# because a blank line starts a new block and a body may hold
# unboundedly many. Each level costs ~92 bytes of `<details>`
# boilerplate for the one input byte (`>`) that asked for it, so
# depth-capped-only rendering still amplified a body ~89x: measured
# 2026-09-29, 1.05 MB in gave 93 MB out in 2.4 s, linearly, and the
# 50 MB `parser.MAX_RAW_MESSAGE_BYTES` ceiling put the reachable
# worst case in the gigabytes on a public uncached route. That is
# the same denial of service the depth cap was added for, wearing a
# timeout instead of a 500. Pre-dates the depth cap.
#
# A whole-render budget is what actually bounds it: boilerplate can
# never exceed roughly `512 * 92` bytes however large the body is.
#
# 512 against a worst-observed 44: measured 2026-09-29 over the same
# 1,000 messages, where the p99 is 14 and the deepest single message
# rendered 44 levels in total.
MAX_QUOTE_LEVELS_PER_RENDER = 512


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


def _reclassify_orphaned_diffs(blocks: list[_Block]) -> None:
    """Promote quoted text blocks that are really hunk bodies.

    Applied to the block list of QUOTED content only, which is what
    scopes `looks_like_orphaned_diff` to the case it is safe for.
    Mutating `kind` rather than special-casing the renderer keeps
    one answer to "is this a diff" for every consumer downstream:
    both the diff renderer and the hunk-quote fold read the same
    field, so a fragment that highlights as a hunk also folds as one.

    The block list is local to one quote's render and is never
    cached or shared, so mutating it in place has no reach beyond
    the call that built it.
    """
    for block in blocks:
        if block.kind == "text" and looks_like_orphaned_diff(block.lines):
            block.kind = "diff"
            block.headerless = True


@dataclass(slots=True)
class _RenderContext:
    msgid_urls: dict[str, str]
    address_redactor: Callable[[str], str] | None = None
    parent_url: str | None = None
    lore_mirror_urls: dict[str, str] | None = None
    # Quote levels left for this render. One context per `render_body`
    # call, threaded through the recursion, because the bound is a
    # property of the whole message rather than of any one block.
    remaining: int = MAX_QUOTE_LEVELS_PER_RENDER


def _flatten_quote(
    block: _Block,
    context: _RenderContext,
    reason: str,
) -> str:
    """Render a quote without descending into it.

    Strips ALL remaining quote markers before handing the text on,
    rather than leaving them in place. That is not cosmetic:
    `linkify` applies the DCO address redactor only to lines
    `_TRAILER_LINE_RE` matches, and that regex is anchored on a
    trailer key at line start, so leaving the prefixes on means no
    line rendered this way is ever redacted. Measured: a
    `Signed-off-by:` holding `alice@localhost` is redacted at depth 2
    and was printed verbatim at depth 65, because the Message-ID
    linkifier that masks this for an ordinary address wants a dotted
    alphabetic TLD. Stripping restores the anchor, so the flattened
    text gets the same redaction and linkification as any other text
    block.

    `reason` replaces the markers in the summary, so the reader is
    told why the nesting stopped being drawn rather than silently
    seeing less of it.
    """
    flat = "\n".join(QUOTE_PREFIX_RE.sub("", line, count=1) for line in block.lines)
    return (
        f"<details><summary><small><em>{reason}</em></small></summary>"
        '<blockquote><pre class="body-text-block">'
        f"{linkify(flat, context.msgid_urls, context.address_redactor, context.lore_mirror_urls)}"
        "</pre></blockquote></details>"
    )


def _render_block(
    block: _Block,
    context: _RenderContext,
    depth: int = 0,
) -> str:
    if block.kind == "quote":
        if depth >= MAX_QUOTE_DEPTH:
            return _flatten_quote(
                block,
                context,
                f"quoting continues past {MAX_QUOTE_DEPTH} levels",
            )
        if context.remaining <= 0:
            return _flatten_quote(
                block,
                context,
                "quoting not expanded further in this message",
            )
        context.remaining -= 1
        stripped = [_strip_one_quote_level(line) for line in block.lines]
        inner = "\n".join(stripped)
        inner_blocks = parse_blocks(inner)
        # Quoted content only: see `_reclassify_orphaned_diffs`. Done
        # here rather than inside `parse_blocks` because "am I inside
        # a quote" is the renderer's knowledge, and done BEFORE the
        # `is_hunk_quote` test below so a promoted fragment folds as
        # the hunk it is.
        _reclassify_orphaned_diffs(inner_blocks)
        inner_html = "".join(_render_block(b, context, depth + 1) for b in inner_blocks)
        # A first-level quote containing a diff is the patch-review
        # "quoted hunk" pattern: a reviewer pasting a chunk of the
        # parent patch to comment on it. Without folding, deep reviews
        # turn the page into wall-of-diff that buries the inline
        # commentary. Wrap in <details> by default and surface a
        # "↗ jump to hunk" link to the parent message (where the
        # original hunk lives in context).
        is_hunk_quote = depth == 0 and any(b.kind == "diff" for b in inner_blocks)
        if is_hunk_quote:
            jump = ""
            if context.parent_url:
                jump = (
                    ' <a href="'
                    + html.escape(context.parent_url)
                    + '">↗ jump to hunk</a>'
                )
            return (
                '<details class="hunk-quote"><summary>'
                f"<small><em>quoted hunk</em>{jump}</small></summary>"
                f"<blockquote>{inner_html}</blockquote></details>"
            )
        if depth + 1 >= QUOTE_COLLAPSE_AT_DEPTH:
            # Wrap deep quotes in <details> so the user can collapse the
            # nested levels. Pico styles details/summary out of the box.
            return (
                "<details><summary><small><em>quoted</em></small></summary>"
                f"<blockquote>{inner_html}</blockquote></details>"
            )
        return f"<blockquote>{inner_html}</blockquote>"

    if block.kind == "diff":
        # Anchors only at top level; nested (inside a quote block)
        # diffs would collide on `h-N` / `h-N-LM` with the primary
        # patch's anchors on the same page.
        # A headerless block never gets anchors, structurally rather
        # than by coincidence. It is a fragment synthesised from
        # quoted text, so its hunk is not a real hunk of this
        # message's patch and `h-1` would collide with one that is.
        # Today `depth == 0` already excludes it (promotion only
        # happens to quoted content); this states the rule rather
        # than relying on the two staying aligned.
        return _render_diff_block(
            block.lines,
            with_anchors=(depth == 0 and not block.headerless),
            headerless=block.headerless,
        )

    if block.kind == "code":
        code_text = "\n".join(block.lines)
        # Clamp giant blocks to TextLexer so a crafted body can't pin
        # a gunicorn worker on a pathological regex run. The block
        # still renders (in monospace), just without language tokens.
        if len(code_text) > PYGMENTS_MAX_BLOCK_CHARS:
            lexer = TextLexer()
        else:
            lexer = _lexer_for_fence(block.info)
        return highlight(code_text, lexer, _CODE_FORMATTER)

    text = "\n".join(block.lines)
    return (
        '<pre class="body-text-block">'
        f"{linkify(text, context.msgid_urls, context.address_redactor, context.lore_mirror_urls)}</pre>"
    )


def render_body(
    body: str | None,
    msgid_urls: dict[str, str] | None = None,
    address_redactor=None,
    parent_url: str | None = None,
    lore_mirror_urls: dict[str, str] | None = None,
) -> Markup:
    """Render `body` to HTML.

    `parent_url`: if set, first-level quote blocks that contain a
    diff (patch-review "quoted hunk" pattern) are wrapped in a
    `<details>` with a "↗ jump to hunk" link pointing here. Pass
    the URL of the message being replied to (typically resolved
    from `article.thread_parent` via the thread's URL map).

    `lore_mirror_urls`: optional `{msgid: mimir_url}` dict. Body
    text linking out to `lore.kernel.org/<slug>/<msgid>/...` gets a
    `(local)` mirror suffix when the embedded msgid is in the dict,
    so the reader can stay on-site without losing the original lore
    URL. The caller resolves msgids globally (any inbox, routed to
    the canonical URL), see `mimir.web.routes.message`.
    """
    if not body:
        return Markup("")
    context = _RenderContext(
        msgid_urls=msgid_urls or {},
        address_redactor=address_redactor,
        parent_url=parent_url,
        lore_mirror_urls=lore_mirror_urls,
    )
    return Markup("".join(_render_block(b, context) for b in parse_blocks(body)))
