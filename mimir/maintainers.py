"""Parser for the kernel `MAINTAINERS` file.

`MAINTAINERS` is a plain-text file at the root of Linus's tree
(https://git.kernel.org/.../linux.git/tree/MAINTAINERS) listing every
recognised subsystem with its maintainers, reviewers, lists, file
globs, and a short status code. The format is line-oriented and only
loosely structured; see the `Description of section entries` block
at the top of the file itself for the canonical spec.

Shape we care about:

    SUBSYSTEM NAME
    M:    Maintainer Name <addr@example.com>
    R:    Reviewer Name <addr@example.com>
    L:    list-name@vger.kernel.org
    S:    Maintained
    F:    fs/foo/
    F:    include/uapi/linux/foo.h
    X:    fs/foo/legacy/

Each block is preceded by a section title (a plain line, no leading
tag), followed by `<tag>:<space><value>` lines. A blank line ends the
block. The header block at the top of the file (before the first
section) is documentation and is skipped.

This module is *pure*: it takes the raw `bytes` of the file and
returns dataclass-shaped data. The caller is responsible for the
git-blob fetch (`mimir.indexnow`-style integration in `cli.py`) and
for replacing the DB rows transactionally.
"""

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class MaintainerEntry:
    """One maintainer or reviewer entry. `role` is `'M'` (maintainer)
    or `'R'` (reviewer); the spec also defines `L:` for lists but those
    don't carry a person, they're list addresses — handled separately
    on the parent Subsystem."""
    role: str
    name: str
    address: str


@dataclass
class Subsystem:
    """One parsed MAINTAINERS section. `status` is the `S:` field
    (`Supported`, `Maintained`, `Odd Fixes`, `Orphan`, `Obsolete`);
    None when the section omits the tag (rare; some legacy sections
    do)."""
    name: str
    status: str | None = None
    maintainers: list[MaintainerEntry] = field(default_factory=list)
    lists: list[str] = field(default_factory=list)
    # Include / exclude path patterns. We keep the raw MAINTAINERS-shaped
    # strings — the caller (a future glob-matcher) decides how to interpret
    # trailing-slash directory semantics, brace expansion, etc. Storing the
    # literal makes the DB row a faithful echo of the source file.
    files: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)


# `Description of section entries:` is the literal heading at the top
# of every MAINTAINERS file marking the end of the preamble and the
# start of the parseable sections. Catching the exact phrase keeps the
# parser robust to whatever wording the doc block grows over time —
# we only need to know "the section list starts after this line."
PREAMBLE_TERMINATOR = "Description of section entries:"


# Tags we parse out of each block. Everything else (`B:`, `C:`, `P:`,
# `T:`, `W:`, `Q:`, `K:`, `N:`, etc.) is skipped silently — they don't
# feed any current consumer and the parser shouldn't fail on unknown
# tags. The `T:` (SCM tree URL) and `B:` (bug-tracker URL) entries are
# the most interesting unparsed ones; revisit if a downstream surface
# wants them.
_KNOWN_TAGS = frozenset({"M", "R", "L", "S", "F", "X"})


def _split_addr(raw: str) -> tuple[str, str] | None:
    """Split `Foo Bar <foo@bar.example>` → `('Foo Bar', 'foo@bar.example')`.

    Returns None if the input isn't shaped like `<name> <<addr>>`. Bare
    addresses (no display name) come back as `('', addr)`; that
    matches how the upstream MAINTAINERS file occasionally writes
    list-shaped or anonymous entries. Anything that doesn't carry an
    `<addr>` at all is rejected — the calling parser drops it rather
    than fabricating a half-row.

    Robust against trailing whitespace and against display names that
    include parenthesised qualifiers like `Foo Bar (FREENAME)`.
    """
    raw = raw.strip()
    open_idx = raw.rfind("<")
    close_idx = raw.rfind(">")
    if open_idx == -1 or close_idx == -1 or close_idx <= open_idx + 1:
        return None
    address = raw[open_idx + 1 : close_idx].strip()
    name = raw[:open_idx].strip()
    # Strip a parenthesised qualifier off the end of the display name,
    # e.g. `Tony Luck (FREENAME)` → `Tony Luck`. The qualifier is
    # documentation, not part of the human-name identity.
    if name.endswith(")"):
        paren_open = name.rfind("(")
        if paren_open > 0:
            name = name[:paren_open].strip()
    if not address:
        return None
    return name, address


def parse(blob: bytes) -> list[Subsystem]:
    """Parse a MAINTAINERS file's raw bytes into a list of Subsystem
    entries, source-order preserved. Decoding is UTF-8 with surrogate-
    escape so non-decodable bytes don't crash the parser; in practice
    MAINTAINERS is ASCII apart from a handful of contributor names
    with non-ASCII characters."""
    text = blob.decode("utf-8", errors="surrogateescape")
    lines = text.splitlines()

    # Walk past the preamble. Every released kernel since the
    # MAINTAINERS file was introduced carries the terminator; if it's
    # ever missing we fall back to "start from line 0" which produces
    # garbage entries from the doc text but doesn't raise.
    start = 0
    for i, line in enumerate(lines):
        if PREAMBLE_TERMINATOR in line:
            start = i + 1
            break

    subsystems: list[Subsystem] = []
    current: Subsystem | None = None

    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            # Blank line ends the current section. The next non-blank
            # line that doesn't carry a tag opens a new section.
            current = None
            continue

        # Indented lines are documentation, table-of-contents continuation,
        # or legacy multi-line tag continuations. Real section content
        # (titles AND tag lines) is anchored at column 0. Skipping
        # indented lines wholesale dodges three classes of false
        # positive in one rule: doc-table examples (`    M: ...`),
        # the `Maintainers List` heading's underline, and any future
        # indented prose.
        if line and (line[0] == " " or line[0] == "\t"):
            continue

        # Tag lines look like `M:\tName <addr>`, anchored at column 0
        # with a single-letter uppercase tag.
        if (
            len(line) >= 2
            and line[1] == ":"
            and line[0].isalpha()
            and line[0].isupper()
        ):
            if current is None:
                # Stray tag line outside any section — usually means
                # the preamble terminator wasn't where we expected.
                # Skip silently rather than coercing into a fake
                # section; the file's structure is the source of
                # truth, not our heuristic.
                continue
            tag = line[0]
            value = line[2:].strip()
            _apply_tag(current, tag, value)
            continue

        # Non-tag, non-blank, column-0 line: a section title.
        # The `Maintainers List` heading would land here too, but
        # since it isn't followed by any tag lines (the underline is
        # indented and skipped, then a blank line, then real
        # sections), the resulting empty Subsystem would be a
        # harmless ghost. Filter it out by requiring at least one
        # tag line before considering a section real.
        current = Subsystem(name=stripped)
        subsystems.append(current)

    # Drop sections that ended up with zero tag content — they're
    # documentation headings we couldn't distinguish from real
    # sections at line-scan time (e.g. "Maintainers List").
    return [s for s in subsystems if s.maintainers or s.files or s.lists or s.status]


def _apply_tag(sub: Subsystem, tag: str, value: str) -> None:
    """Mutate `sub` in place from one `<tag>:<value>` line."""
    if tag not in _KNOWN_TAGS:
        return
    if tag == "S":
        sub.status = value or None
        return
    if tag == "F":
        sub.files.append(value)
        return
    if tag == "X":
        sub.excludes.append(value)
        return
    if tag == "L":
        # `L:` carries a list address, sometimes with trailing
        # qualifiers in parentheses (e.g. "list@vger.kernel.org
        # (moderated for non-subscribers)"). Strip the parenthesised
        # tail; the canonical address is what we store.
        addr = value.split("(", 1)[0].strip()
        if addr:
            sub.lists.append(addr)
        return
    if tag in ("M", "R"):
        parsed_addr = _split_addr(value)
        if parsed_addr is None:
            return
        name, address = parsed_addr
        sub.maintainers.append(
            MaintainerEntry(role=tag, name=name, address=address)
        )


def iter_subsystem_paths(subsystems: Iterable[Subsystem]) -> Iterable[tuple[Subsystem, str, bool]]:
    """Yield `(subsystem, path_pattern, is_exclude)` triples — handy
    for bulk-inserting into the `subsystem_paths` table without each
    caller writing the same nested loop."""
    for sub in subsystems:
        for path in sub.files:
            yield sub, path, False
        for path in sub.excludes:
            yield sub, path, True
