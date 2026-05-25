"""Body segmentation: walk lines, group runs of the same kind into
discrete blocks (text / quote / diff / code) for the renderer to
dispatch on.

`parse_blocks` is the only public entry. Its output is a list of
`_Block` dataclasses that the orchestrator in `body.py` walks
recursively (quote blocks re-enter `parse_blocks` after one level
of `>`-prefix stripping, which is how nested quotes render).

Diff handling lives here only at the segmentation level, "this run
of lines is one diff block." Per-hunk rendering, lexer choice, and
hunk anchors are in `diff.py`.
"""

import re
from dataclasses import dataclass, field

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

    # Strip trailing email-scissors lines off diff blocks. `b4 send`
    # (and various manual workflows) put a bare `---` between the last
    # hunk and the `base-commit:` / `change-id:` trailer block; the
    # main-loop diff-continuation rule (line starts with " +-") pulls
    # that line into the diff, which Pygments then renders as a
    # `-<operator '--'>` ghost-deletion at the end of the patch. A
    # *legitimate* `---` hunk-line (delete prefix + literal `--`
    # content) is always followed by more diff content, so the
    # "last-line" check distinguishes the two cases without look-ahead.
    i = 0
    while i < len(blocks):
        block = blocks[i]
        if (
            block.kind == "diff"
            and block.lines
            and block.lines[-1].rstrip() == "---"
        ):
            scissors = block.lines.pop()
            if not block.lines:
                blocks.pop(i)
                continue
            if i + 1 < len(blocks) and blocks[i + 1].kind == "text":
                blocks[i + 1].lines.insert(0, scissors)
            else:
                blocks.insert(i + 1, _Block(kind="text", lines=[scissors]))
        i += 1

    return blocks
