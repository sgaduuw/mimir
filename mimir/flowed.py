"""RFC 3676 `text/plain; format=flowed` decoding.

A flowed body encodes two things a plain reader cannot see:

- **Space-stuffing** (§4.4). Any line beginning with a space, `>`,
  or `From ` gets one space prepended on generation. The receiver
  MUST delete it.
- **Soft line breaks** (§4.5). A line ending in a space is a wrap
  the sender's client chose, not a paragraph break, and re-joins
  with the next line. `-- ` is excepted: it is the signature
  delimiter, which also happens to end in a space.

Skipping both is not the graceful degradation it looks like. An
un-stuffed quote arrives as `' > text'`, and `blocks.QUOTE_PREFIX_RE`
is anchored at `^`, so the renderer sees no quote at all: the whole
message becomes one literal text block, with no fold and no diff
detection inside the quoted hunks.

Decoding happens in `parser.parse_message`, alongside charset and
transfer-encoding decoding, so every consumer (rendering, JSON-LD,
related-discussions tokens, patch/trailer extraction) sees plain
text with no plumbing. The git blob is untouched: this is a decode
of what the sender declared, not a transform of the archive.
"""

import re

# The quote prefix, matched AFTER un-stuffing. Generation stuffs the
# already-quoted line (`> x` begins with `>`, so it ships as ` > x`),
# so counting marks first would see the space and read depth 0.
#
# The trailing ` ?` is the separator space of the near-universal
# `"> "` quoting convention, not content. It has to come off before
# joining a soft break, or `"> abc "` + `"> def"` re-joins to
# `"> abc  def"` with a doubled space; and it has to be re-emitted
# verbatim so the output still looks like what the sender wrote.
# Mirrors `blocks.STRIP_ONE_LEVEL_RE`'s `^>\s?` per-level shape.
_PREFIX_RE = re.compile(r"^>+ ?")

# Ends in a space and is therefore shaped like a soft break, but is
# the signature delimiter. Every mail client keys on these exact
# three bytes, so joining it into the preceding line destroys the
# delimiter and swallows the signature into the body.
SIGNATURE_SEPARATOR = "-- "


def unflow(text: str, *, delsp: bool = False) -> str:
    """Decode a `format=flowed` body to plain text.

    `delsp` mirrors the `delsp=yes` content-type parameter: the
    space that marks a soft break is part of the wrap rather than
    the content, so it is dropped instead of kept as the word
    separator when two lines re-join.
    """
    out: list[str] = []
    pending: str | None = None
    pending_prefix = ""

    def flush() -> None:
        nonlocal pending, pending_prefix
        if pending is not None:
            out.append(pending_prefix + pending)
            pending, pending_prefix = None, ""

    for raw in text.splitlines():
        line = raw.removeprefix(" ")  # §4.4 un-stuff
        match = _PREFIX_RE.match(line)
        prefix = match.group(0) if match else ""
        content = line[len(prefix) :]

        # A blank line is a paragraph boundary whatever preceded it,
        # and is itself meaningful (it separates paragraphs), so it
        # terminates any pending join and survives into the output.
        # `rstrip` drops the prefix's separator space on an
        # otherwise-empty quoted line: `"> "` renders as `">"`.
        if not content:
            flush()
            out.append(prefix.rstrip())
            continue

        if content == SIGNATURE_SEPARATOR:
            flush()
            out.append(prefix + content)
            continue

        soft = content.endswith(" ")
        # Hold the paragraph WITHOUT the soft-break space, so a
        # paragraph that ends on a dangling soft marker (end of
        # body, or a depth change) does not emit a trailing space
        # that was only ever a wrap marker.
        core = content[:-1] if soft else content

        # A deeper quote opening after a soft break starts a new
        # paragraph rather than continuing the shallower one.
        if pending is not None and prefix.count(">") > pending_prefix.count(">"):
            flush()

        if pending is None:
            pending, pending_prefix = core, prefix
        else:
            # Joining at the PENDING prefix, which may be deeper than
            # this line's. Strictly §4.5 ends the paragraph on any
            # depth change, but the shape seen in the wild (see
            # tests/test_flowed.py) is a quoted line whose
            # soft-wrapped continuation dropped its `>`, and the
            # trailing space is the sender asserting the join.
            # Honouring it costs nothing when the depths do match.
            pending += ("" if delsp else " ") + core

        if not soft:
            flush()

    flush()
    return "\n".join(out)
