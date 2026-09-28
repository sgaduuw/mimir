"""RFC 3676 `format=flowed` decoding contract.

The damage this prevents is structural rather than cosmetic: an
un-decoded stuffed quote line arrives as `' > text'`, and the body
renderer's quote-prefix match is anchored at `^`, so a whole message
renders as one literal text block with no quote structure, no fold,
and no diff detection inside the quoted hunks.
"""

import time
from email import policy
from email.parser import BytesParser

from mimir.flowed import unflow
from mimir.parser import parse_message


def test_unflow_removes_one_stuffed_space():
    assert unflow(" > quoted") == "> quoted"
    assert unflow("  indented") == " indented"
    assert unflow(" From the top") == "From the top"


def test_unflow_leaves_unstuffed_lines_alone():
    assert unflow("plain text") == "plain text"
    assert unflow("> already unstuffed") == "> already unstuffed"


def test_unflow_joins_a_soft_line_break():
    assert unflow("the quick brown \nfox") == "the quick brown fox"


def test_unflow_keeps_a_hard_line_break():
    assert unflow("the quick brown\nfox") == "the quick brown\nfox"


def test_unflow_joins_within_one_quote_depth_and_keeps_the_prefix():
    """A soft break inside a quote re-joins the CONTENT, not the raw
    lines. Naive concatenation would produce `> abc > def`."""
    assert unflow("> abc \n> def") == "> abc def"
    assert unflow(" >> abc \n >> def") == ">> abc def"


def test_unflow_joins_a_continuation_that_dropped_its_quote_prefix():
    """Observed in the wild (linux-wireless message
    376dbf6f-ecda-49ca-a7c0-af2a4077e945@salmtek.com, 2026-09-03):
    the soft-wrapped continuation of a quoted line arrives at depth
    0 instead of repeating the `>`. RFC 3676 says a depth change
    ends the paragraph, which would leave the sentence visibly split
    mid-word-group; the sender's trailing space asserts continuation,
    so join at the higher depth."""
    out = unflow(" > and the offset of the first \nextension descriptor")
    assert out == "> and the offset of the first extension descriptor"


def test_unflow_does_not_join_into_a_deeper_quote():
    """A deeper quote starting after a soft break is a new
    paragraph, not a continuation."""
    out = unflow("> outer soft \n>> inner")
    assert out == "> outer soft\n>> inner"


def test_unflow_never_joins_the_signature_separator():
    """`-- ` ends in a space but is the signature delimiter, not a
    soft break. Joining it would eat the signature into the last
    body line and destroy the delimiter every mail client keys on."""
    assert unflow("body\n-- \nSender") == "body\n-- \nSender"


def test_unflow_delsp_drops_the_break_space():
    assert unflow("abc \ndef", delsp=True) == "abcdef"
    assert unflow("abc \ndef", delsp=False) == "abc def"


def test_unflow_trailing_soft_line_at_end_of_body_is_emitted():
    """A soft break with nothing after it still has to reach the
    output; dropping the pending paragraph would silently truncate
    the last line of the message."""
    assert unflow("last line ") == "last line"


def test_unflow_blank_line_ends_the_paragraph_and_survives():
    """A blank line terminates any pending join (otherwise the
    accumulator keeps swallowing lines) AND is itself content: it is
    the paragraph separator, so dropping it would merge two
    paragraphs into one."""
    assert unflow("abc \n\ndef") == "abc\n\ndef"
    assert unflow("> abc \n>\n> def") == "> abc\n>\n> def"


def _message(content_type: str, body: str) -> bytes:
    return (
        b"Message-ID: <flowed@example.test>\r\n"
        b"From: A <a@example.test>\r\n"
        b"Subject: t\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: " + content_type.encode() + b"\r\n"
        b"\r\n" + body.encode()
    )


def test_parse_message_decodes_a_flowed_body():
    raw = _message(
        "text/plain; charset=UTF-8; format=flowed",
        " >> quoted deeply\n >\n > quoted once\n\nreply\n",
    )
    parsed = parse_message(raw)
    assert parsed.body is not None
    assert parsed.body.splitlines()[0] == ">> quoted deeply"
    assert ">" == parsed.body.splitlines()[1]
    assert parsed.body.splitlines()[2] == "> quoted once"


def test_parse_message_leaves_a_non_flowed_body_byte_identical():
    """Space-stuffing is only meaningful under `format=flowed`. A
    plain body with a leading space means a leading space."""
    body = " > not stuffed, just indented\nand a trailing space line \n"
    raw = _message("text/plain; charset=UTF-8", body)
    parsed = parse_message(raw)
    assert parsed.body == body


def test_parse_message_flowed_body_renders_quote_blocks():
    """The consumer-side claim: decoding is what makes the renderer
    see quotes at all. Asserted end to end rather than on `unflow`
    alone, because the bug was that the two never met."""
    from mimir.rendering import render_body

    raw = _message(
        "text/plain; charset=UTF-8; format=flowed",
        " > +\twhile (off > ALIGN) {\n"
        " > +\t\toff -= ALIGN;\n"
        " > +\t\tmemset(&cmd, 0, sizeof(cmd));\n"
        "\nunnecessary cleanup\n",
    )
    parsed = parse_message(raw)
    out = str(render_body(parsed.body))
    assert "<blockquote>" in out
    assert '<div class="highlight">' in out


def test_flowed_param_survives_the_stdlib_parser():
    """Pin the mechanism the decode gates on: `get_content_type()`
    drops parameters, so the gate has to read `get_param`."""
    raw = _message("text/plain; charset=UTF-8; format=flowed", "x\n")
    part = (
        BytesParser(policy=policy.default)
        .parsebytes(raw)
        .get_body(preferencelist=("plain",))
    )
    assert part is not None
    assert part.get_content_type() == "text/plain"
    assert part.get_param("format") == "flowed"


def test_unflow_whitespace_only_line_is_not_a_soft_break():
    """A line holding nothing but whitespace has no content for a
    following line to continue, so it is a paragraph boundary even
    though it ends in a space.

    This is not a nicety. A patch pasted into a flowed body has
    context lines whose single leading space IS diff structure, and
    an all-whitespace one looks exactly like a soft break. Joining
    it merges two hunk lines into one, at which point the `-` of the
    following line stops being a gutter marker and gets lexed as a
    minus operator in the middle of a line of C. Observed against
    linux-wireless commit 3eb5760f8672.
    """
    body = ">   \n> -\tif (count > size)\n> +\tif (count >= size)"
    assert unflow(body) == body


def test_unflow_joins_space_separated_quote_marks_without_reinserting_them():
    """`_PREFIX_RE` is `^>+ ?`, so it reads `"> > "` as depth 1 with
    content `"> def"`. Joining a soft break then splices the inner
    `>` into the middle of the sentence, which is exactly the
    `"> abc > def"` corruption
    `test_unflow_joins_within_one_quote_depth_and_keeps_the_prefix`
    exists to prevent; that test only covers the `">>"` spelling.

    Space-separated quote marks are not hypothetical for mimir: the
    renderer's own `blocks.QUOTE_PREFIX_RE` is `^((?:>\\s?)+)` and
    scores `"> > abc"` as depth 2, so the two modules disagree about
    what the quote prefix of the same line is.
    """
    from mimir.rendering.blocks import _quote_depth

    # Precondition: the renderer treats this spelling as depth 2, so
    # the string is a real quote prefix and not content.
    assert _quote_depth("> > abc") == 2

    assert unflow("> > abc \n> > def") == "> > abc def"


def test_unflow_is_linear_in_line_count():
    """`pending += ... + core` accumulates into a variable that the
    nested `flush()` closes over, so it is a cell (STORE_DEREF) and
    CPython's in-place string-concatenation specialisation, which
    only applies to STORE_FAST locals, does not fire. The join is
    therefore O(n^2) in the size of a soft-wrapped paragraph.

    `parse_message` runs on the read path for every message view, and
    caps the raw message at 50 MB, so a single crafted flowed body of
    a few MB pins a gunicorn worker to its 60 s timeout on every
    request.

    Scale-invariant assertion (a ratio, not a wall-clock budget) so
    this does not become a flaky machine-speed test. Linear work
    doubles; measured on this branch the ratio is ~3.4.
    """

    def build(n: int) -> str:
        return "\n".join(f"word{i:06d} " for i in range(n)) + "\nend"

    def best_of(n: int, rounds: int = 3) -> float:
        text = build(n)
        unflow(text)  # warm
        return min(_timed(text) for _ in range(rounds))

    def _timed(text: str) -> float:
        start = time.perf_counter()
        unflow(text)
        return time.perf_counter() - start

    small = best_of(30_000)
    large = best_of(60_000)
    # Precondition: the small run is long enough that timer noise is
    # not what the ratio measures.
    assert small > 0.005, f"baseline too fast to compare: {small}s"
    assert large / small < 2.6, (
        f"doubling the input multiplied the work by {large / small:.2f}x "
        f"({small * 1000:.1f}ms -> {large * 1000:.1f}ms); expected ~2x"
    )


def test_parse_message_honours_delsp_yes():
    """`unflow(delsp=True)` is covered directly, but nothing pinned
    that `parse_message` READS the parameter. A mutant hardcoding
    `delsp=False` at the call site survived the suite."""
    raw = _message(
        "text/plain; charset=UTF-8; format=flowed; delsp=yes",
        "abc \ndef\n",
    )
    assert parse_message(raw).body == "abcdef\n"

    raw_plain = _message("text/plain; charset=UTF-8; format=flowed", "abc \ndef\n")
    assert parse_message(raw_plain).body == "abc def\n"


def test_unflow_does_not_rewrite_exotic_line_separators_into_newlines():
    """`str.splitlines()` breaks on form feed, U+2028 and U+0085, and
    the output is rejoined with `\\n`, so using it would REWRITE those
    characters. A form feed is a real section separator in kernel
    sources, so a quoted patch would gain a line break mid-hunk and
    the following line would lose its gutter marker."""
    for sep in ("\x0c", " ", "\x85"):
        body = f"+ before{sep}after"
        assert unflow(body) == body, f"rewrote {sep!r}"


def test_unflow_preserves_a_trailing_newline():
    """`split("\n")` keeps the empty final element that a trailing
    newline produces, where `splitlines()` would swallow it. Pinned
    because a non-flowed body keeps its trailing newline and a
    flowed one has to agree: the decode is not allowed to change the
    shape of the text in ways unrelated to flowing."""
    assert unflow("one\ntwo\n") == "one\ntwo\n"
    assert unflow("one\ntwo") == "one\ntwo"
