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
# The optional space after each mark is the separator of the
# near-universal `"> "` convention, not content. It has to come off
# before joining a soft break, or `"> abc "` + `"> def"` re-joins to
# `"> abc  def"` with a doubled space, and `"> > abc "` + `"> > def"`
# re-joins to `"> > abc > def"` with a quote mark spliced into the
# middle of a sentence.
#
# Per-mark rather than once at the end, so this agrees with
# `blocks.QUOTE_PREFIX_RE` (`^((?:>\s?)+)`) about where the prefix of
# a given line ends. They must: that module decides how deep a line
# is quoted, and this one decides which lines may be joined, so a
# disagreement means joining across a depth boundary the renderer
# then draws.
_PREFIX_RE = re.compile(r"^(?:> ?)+")

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
    # The paragraph under construction, held as a list of fragments
    # rather than a growing str. A closure would make `pending` a
    # CELL variable, and CPython's in-place unicode-concat
    # specialisation only fires on STORE_FAST, so `pending += core`
    # would copy the whole paragraph every line: measured 3.4x per
    # doubling of the input. `parse_message` runs on the read path
    # for every message view, so that is a request-time cost on a
    # body an outsider controls, not just a slow batch job.
    pending: list[str] | None = None
    pending_prefix = ""

    def flush() -> None:
        nonlocal pending, pending_prefix
        if pending is not None:
            out.append(pending_prefix + "".join(pending))
            pending, pending_prefix = None, ""

    # `split("\n")` rather than `splitlines()`: the latter also
    # breaks on form feed, U+2028, U+0085 and friends, and since the
    # output is rejoined with "\n" those characters would be
    # REWRITTEN into newlines. A form feed is a real section
    # separator in kernel sources, so a patch quoted in a flowed
    # message would gain a line break mid-hunk and the following
    # line would lose its gutter marker.
    for raw in text.split("\n"):
        line = raw.removeprefix(" ")  # §4.4 un-stuff
        match = _PREFIX_RE.match(line)
        prefix = match.group(0) if match else ""
        content = line[len(prefix) :]

        # A line with nothing but whitespace is a paragraph boundary
        # whatever preceded it: there is no content for a following
        # line to continue, so it is not a soft break however it
        # ends. It is also meaningful in its own right, twice over.
        # Empty, it separates paragraphs. All-whitespace, it is very
        # often a diff CONTEXT line inside a patch someone pasted
        # into a flowed message, where the leading space is
        # structure; joining it merges two hunk lines and the next
        # line's `-` stops reading as a gutter marker. So emit it
        # verbatim, except that an otherwise-empty quoted line sheds
        # the prefix's separator space (`"> "` renders as `">"`).
        if not content.strip():
            flush()
            out.append(prefix + content if content else prefix.rstrip())
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
            pending, pending_prefix = [core], prefix
        else:
            # Joining at the PENDING prefix, which may be deeper than
            # this line's. Strictly §4.5 ends the paragraph on any
            # depth change, but the shape seen in the wild (see
            # tests/test_flowed.py) is a quoted line whose
            # soft-wrapped continuation dropped its `>`, and the
            # trailing space is the sender asserting the join.
            # Honouring it costs nothing when the depths do match.
            if not delsp:
                pending.append(" ")
            pending.append(core)

        if not soft:
            flush()

    flush()
    return "\n".join(out)
