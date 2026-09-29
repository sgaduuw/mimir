"""RFC 3676 `format=flowed` decoding contract.

The damage this prevents is structural rather than cosmetic: an
un-decoded stuffed quote line arrives as `' > text'`, and the body
renderer's quote-prefix match is anchored at `^`, so a whole message
renders as one literal text block with no quote structure, no fold,
and no diff detection inside the quoted hunks.
"""

import re
from email import policy
from email.parser import BytesParser

from mimir.flowed import _PREFIX_RE, SIGNATURE_SEPARATOR, unflow
from mimir.parser import parse_message
from mimir.rendering.blocks import QUOTE_PREFIX_RE, _quote_depth


def _lines(text):
    """Split on the line endings mail uses, so a CRLF assertion
    compares content and not which ending survived. Deliberately
    NOT `splitlines()`: that would also swallow a form feed and
    hide the separator-preservation guard above."""
    return text.replace("\r\n", "\n").split("\n")


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
    # Precondition: the renderer treats this spelling as depth 2, so
    # the string is a real quote prefix and not content.
    assert _quote_depth("> > abc") == 2

    assert unflow("> > abc \n> > def") == "> > abc def"


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


# CRLF is the canonical wire form for mail, and `get_content()`
# hands it through verbatim. Splitting on "\n" alone leaves a
# trailing "\r" that makes every soft-break and delimiter test
# below it false, i.e. silently turns the decoder off.


def test_unflow_joins_a_soft_break_in_a_crlf_body():
    # Precondition: the LF spelling of the exact same body IS joined,
    # so the line terminator is the only variable under test.
    assert unflow("the quick brown \nfox") == "the quick brown fox"

    assert _lines(unflow("the quick brown \r\nfox"))[0] == "the quick brown fox"


def test_unflow_joins_a_quoted_soft_break_in_a_crlf_body():
    assert unflow("> abc \n> def") == "> abc def"  # precondition, LF form

    assert _lines(unflow("> abc \r\n> def"))[0] == "> abc def"


def test_unflow_keeps_the_signature_separator_on_its_own_line_under_crlf():
    """Red today for a THIRD reason, and a guard the fix must also
    keep green.

    Observed failure is `['body ', '-- ', 'sig']`: `soft` is False so
    the dangling soft-break space is emitted as content, which the LF
    path deliberately strips (`core = content[:-1] if soft`). Trailing
    whitespace is not cosmetic here, it is what a diff context line
    and a `-- ` delimiter are made of.

    `content == SIGNATURE_SEPARATOR` is broken by the same trailing
    `\\r` (`"-- \\r" != "-- "`). That is invisible today only because a
    CRLF body never joins anything, so there is nothing for the
    signature to be swallowed into; a fix that restores the join
    without also fixing the delimiter comparison turns this red
    again, which is why it is written down before the fix exists."""
    assert SIGNATURE_SEPARATOR == "-- "
    # Precondition: the LF spelling has a preceding soft break, so the
    # delimiter really is a join candidate and not trivially safe.
    assert _lines(unflow("body \n-- \nsig")) == ["body", "-- ", "sig"]

    assert _lines(unflow("body \r\n-- \r\nsig")) == ["body", "-- ", "sig"]


def test_parse_message_decodes_a_crlf_flowed_body_end_to_end():
    """The production shape: a base64 text/plain part, which is the
    transfer encoding that preserves CRLF verbatim (all 6 CRLF bodies
    in the 400-message linux-wireless sample are base64)."""
    import base64

    payload = base64.b64encode(b"the quick brown \r\nfox jumps\r\n")

    def message(ctype: bytes) -> bytes:
        return (
            b"Message-ID: <crlf@example.test>\r\n"
            b"From: A <a@example.test>\r\n"
            b"Subject: t\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: " + ctype + b"\r\n"
            b"Content-Transfer-Encoding: base64\r\n"
            b"\r\n" + payload + b"\r\n"
        )

    # Precondition: the stdlib really does hand this part's body to
    # `unflow` with the CR still attached. Asserted on the NON-flowed
    # spelling of the identical part, which `parse_message` passes
    # through untouched, so the precondition cannot be satisfied by
    # the behaviour under test.
    untouched = parse_message(message(b"text/plain; charset=UTF-8"))
    assert untouched.body == "the quick brown \r\nfox jumps\r\n"

    parsed = parse_message(message(b"text/plain; charset=UTF-8; format=flowed"))
    assert parsed.body is not None
    assert _lines(parsed.body)[0] == "the quick brown fox jumps"


def test_flowed_and_blocks_agree_on_where_a_quote_prefix_ends():
    """`flowed.py`'s comment on `_PREFIX_RE`: "Per-mark rather than
    once at the end, so this agrees with `blocks.QUOTE_PREFIX_RE`
    (`^((?:>\\s?)+)`) about where the prefix of a given line ends.
    They must: that module decides how deep a line is quoted, and
    this one decides which lines may be joined, so a disagreement
    means joining across a depth boundary the renderer then draws."

    They do not agree. `QUOTE_PREFIX_RE` separates marks with `\\s?`
    (any whitespace); `_PREFIX_RE` uses a literal space. A tab
    between marks is read as depth 2 by the renderer and depth 1 by
    the decoder, which is precisely the joining-across-a-drawn-
    boundary case the comment says cannot happen.

    Pre-existing (the old `^>+ ?` had the same gap), so this is not a
    regression introduced by 65cc600; it is a new comment and a new
    test docstring asserting an invariant that was never true. Fix
    the regex to `^(?:>\\s?)+` or stop claiming agreement.
    """
    # Precondition: the two patterns are the ones named in the comment.
    assert _PREFIX_RE.pattern == r"^(?:>\s?)+"
    assert QUOTE_PREFIX_RE.pattern == r"^((?:>\s?)+)"

    disagreements = []
    for line in ("> x", "> > x", ">> x", ">>> x", ">  > x", ">\tx", ">\t> x", "> >\tx"):
        m = _PREFIX_RE.match(line)
        flowed_depth = m.group(0).count(">") if m else 0
        if flowed_depth != _quote_depth(line):
            disagreements.append((line, flowed_depth, _quote_depth(line)))
    assert not disagreements, (
        "flowed._PREFIX_RE and blocks.QUOTE_PREFIX_RE disagree on "
        f"(line, flowed_depth, blocks_depth): {disagreements}"
    )


def test_a_tab_separated_quote_mark_is_not_joined_across_the_drawn_depth():
    """The consequence of the disagreement above, end to end: a depth-2
    line is folded into a depth-1 paragraph by the soft-break join,
    so the nested blockquote the renderer would have drawn is gone."""
    joined = unflow("> abc \n>\t> def")
    # Precondition: the renderer scores the second line as deeper.
    assert _quote_depth("> abc") == 1
    assert _quote_depth(">\t> def") == 2

    assert len(joined.split("\n")) == 2, (
        f"depth-2 line was joined into the depth-1 paragraph: {joined!r}"
    )
    assert not re.search(r"\S\s+>\s", joined.split("\n")[0]), (
        f"a quote mark was spliced into the middle of a line: {joined!r}"
    )


def test_unflow_blank_quoted_line_sheds_the_prefix_separator_space():
    """`"> "` with nothing after it is a blank quoted line, and the
    separator space is prefix decoration rather than content, so it
    comes off. Covers the `"> "` spelling specifically: the
    paragraph-boundary guard above uses `">"`, where the rstrip is a
    no-op and a mutant dropping it survives."""
    assert unflow("> abc\n> \n> def") == "> abc\n>\n> def"
