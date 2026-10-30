"""Which paragraphs of a mail body may re-flow on a phone (#989).

Mail is hard-wrapped at about 72 columns; a phone wraps it again at
about 40, which staircases it. The renderer wraps a paragraph that
qualifies in `<span class="reflow">`, and a phone-only CSS rule joins its
lines. Desktop keeps the breaks; one HTML, one ETag. Display only: the
git blob is untouched.

Conservative by design (owner, 2026-10-29): when unsure, leave it alone.
Lists, trailers, tables, ASCII art, commit lists, indented lines and
lines with no space (paths, base64) keep their breaks, and a short line
in the middle of a paragraph is a deliberate break. The scan is linear in the text (AGENTS.md test rule 7).
"""

import re

# A hard-wrapped line, every line of the paragraph but its last.
MIN_WRAPPED = 50
MAX_WRAPPED = 100

_LIST = re.compile(r"(?:[-*+]|\d+[.)]|[A-Za-z][.)])\s")
_TRAILER = re.compile(r"[A-Za-z][A-Za-z-]*:\s")
_URL_ONLY = re.compile(r"\S+://\S+")
_BOX = re.compile("[─-╿]")
# Two spaces not after a sentence end: aligned columns, not prose.
_ALIGNED = re.compile(r"(?<![.?!:])  ")
# A commit list: an abbreviated hash, then the subject.
_HASH = re.compile(r"[0-9a-f]{7,}\b")


def reflowable(lines: list[str]) -> bool:
    """True when this paragraph (a run of non-blank lines) is plain
    hard-wrapped prose that may be joined on a phone."""
    if len(lines) < 2:
        return False
    last = len(lines) - 1
    for i, line in enumerate(lines):
        if not line or line[0].isspace() or len(line) > MAX_WRAPPED:
            return False
        # Every line but the last is near the wrap width and has spaces;
        # a line with none is a path, base64 or a long identifier. The last
        # line may be short, or a single word.
        if i < last and (len(line) < MIN_WRAPPED or " " not in line):
            return False
        if "|" in line or "+--" in line:
            return False
        if (
            _LIST.match(line)
            or _TRAILER.match(line)
            or _HASH.match(line)
            or _URL_ONLY.fullmatch(line)
            or _BOX.search(line)
            or _ALIGNED.search(line)
        ):
            return False
    return True
