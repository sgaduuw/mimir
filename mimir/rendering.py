import html
import re
from dataclasses import dataclass, field

from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import DiffLexer

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
# line) is part of the canonical patch payload — copy-paste-to-patch
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
# and emit `[off-list ref]` — that actively looks like broken
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
_EMAIL_ANGLE_RE = re.compile(r"<([^>@\s]+@[^>@\s]+)>")

_DIFF_LEXER = DiffLexer()
# Class-based output so the diff inherits the page theme via CSS.
# Inline styles never adapt to dark mode; classes pair with the CSS
# block in base.html that uses Pico's variables + light-dark() so
# add/remove colours stay legible in both modes.
_DIFF_FORMATTER = HtmlFormatter(noclasses=False, nobackground=True, cssclass="highlight")


@dataclass
class _Block:
    kind: str  # "text" | "quote" | "diff"
    lines: list[str] = field(default_factory=list)


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

    def push(kind: str, line: str) -> None:
        if blocks and blocks[-1].kind == kind:
            blocks[-1].lines.append(line)
        else:
            blocks.append(_Block(kind=kind, lines=[line]))

    for raw_line in text.splitlines():
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

    Message-IDs are *not* rendered verbatim — they're email-shaped (often
    with @host, sometimes with the literal local-part), and rendering
    them in body text would re-leak the address info we already
    de-leaked from URLs. Instead they collapse to a neutral placeholder:
    `[ref]` (with link, when in archive) or `[off-list ref]` (when not).
    The link still points at the canonical archive URL.

    `address_redactor` is an optional callable ``(email_str) -> str``
    applied to ``<email>`` patterns on DCO trailer lines
    (``Signed-off-by:``, ``Reviewed-by:``, etc.). The callable returns
    the replacement to emit verbatim (already including the angle
    brackets it wants displayed); typically the web-layer wrapper
    returns ``<addr@dom>`` for allowlisted senders and ``<redacted>``
    otherwise. Without a redactor, trailer lines fall through to the
    msgid linkifier and emit ``[off-list ref]`` which actively looks
    like broken DCO metadata.
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
    elsewhere on the line still get the normal URL treatment."""
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
        spans.append((m.start(), m.end(), redactor(m.group(1))))
    spans.sort()
    for start, end, replacement in spans:
        out.append(html.escape(line[pos:start]))
        out.append(replacement)
        pos = end
    out.append(html.escape(line[pos:]))
    return "".join(out)


QUOTE_COLLAPSE_AT_DEPTH = 2  # depth at which nested quotes auto-collapse


def _render_block(
    block: _Block,
    msgid_urls: dict[str, str],
    address_redactor=None,
    depth: int = 0,
) -> str:
    if block.kind == "quote":
        stripped = [_strip_one_quote_level(line) for line in block.lines]
        inner = "\n".join(stripped)
        inner_html = "".join(
            _render_block(b, msgid_urls, address_redactor, depth + 1)
            for b in parse_blocks(inner)
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
        diff_text = "\n".join(block.lines)
        return highlight(diff_text, _DIFF_LEXER, _DIFF_FORMATTER)

    text = "\n".join(block.lines)
    return (
        '<pre style="white-space: pre-wrap; word-wrap: break-word; margin: 0;">'
        f"{linkify(text, msgid_urls, address_redactor)}</pre>"
    )


def render_body(
    body: str | None,
    msgid_urls: dict[str, str] | None = None,
    address_redactor=None,
) -> Markup:
    if not body:
        return Markup("")
    return Markup(
        "".join(
            _render_block(b, msgid_urls or {}, address_redactor)
            for b in parse_blocks(body)
        )
    )
