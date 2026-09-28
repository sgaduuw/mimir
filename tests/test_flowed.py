"""RFC 3676 `format=flowed` decoding contract.

The damage this prevents is structural rather than cosmetic: an
un-decoded stuffed quote line arrives as `' > text'`, and the body
renderer's quote-prefix match is anchored at `^`, so a whole message
renders as one literal text block with no quote structure, no fold,
and no diff detection inside the quoted hunks.
"""

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
