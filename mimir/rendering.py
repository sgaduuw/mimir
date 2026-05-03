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
URL_OR_MSGID_RE = re.compile(
    r'(?P<url>https?://[^\s<>"\'\]\)]+)'
    r"|<(?P<msgid>[A-Za-z0-9._%+\-=$/]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>"
)

_DIFF_LEXER = DiffLexer()
_DIFF_FORMATTER = HtmlFormatter(noclasses=True, nobackground=True, style="default")


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
    """
    blocks: list[_Block] = []
    in_diff = False

    def push(kind: str, line: str) -> None:
        if blocks and blocks[-1].kind == kind:
            blocks[-1].lines.append(line)
        else:
            blocks.append(_Block(kind=kind, lines=[line]))

    for raw_line in text.splitlines():
        if _quote_depth(raw_line) > 0:
            in_diff = False
            push("quote", raw_line)
            continue

        if DIFF_START_RE.match(raw_line):
            in_diff = True
            push("diff", raw_line)
            continue

        if in_diff and raw_line and raw_line[0] in " +-":
            push("diff", raw_line)
            continue

        if in_diff and not raw_line:
            # blank line inside a diff: keep, hunks have spacing
            push("diff", raw_line)
            continue

        in_diff = False
        push("text", raw_line)

    return blocks


def linkify(text: str, msgid_urls: dict[str, str] | None = None) -> str:
    """Escape `text` and replace URLs and `<Message-IDs>` with anchor tags.

    `msgid_urls` is a precomputed map from Message-ID to canonical URL
    (built by the view layer with a single bulk SELECT). Message-IDs not
    in the map render as plain text — we don't have enough info to build
    a working URL without their date.
    """
    msgid_urls = msgid_urls or {}
    out: list[str] = []
    pos = 0
    for m in URL_OR_MSGID_RE.finditer(text):
        out.append(html.escape(text[pos : m.start()]))
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
                out.append(
                    f'&lt;<a href="{html.escape(href)}">{html.escape(mid)}</a>&gt;'
                )
            else:
                out.append(f"&lt;{html.escape(mid)}&gt;")
            pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


QUOTE_COLLAPSE_AT_DEPTH = 2  # depth at which nested quotes auto-collapse


def _render_block(
    block: _Block,
    msgid_urls: dict[str, str],
    depth: int = 0,
) -> str:
    if block.kind == "quote":
        stripped = [_strip_one_quote_level(line) for line in block.lines]
        inner = "\n".join(stripped)
        inner_html = "".join(
            _render_block(b, msgid_urls, depth + 1) for b in parse_blocks(inner)
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
        f"{linkify(text, msgid_urls)}</pre>"
    )


def render_body(
    body: str | None,
    msgid_urls: dict[str, str] | None = None,
) -> Markup:
    if not body:
        return Markup("")
    return Markup(
        "".join(_render_block(b, msgid_urls or {}) for b in parse_blocks(body))
    )
