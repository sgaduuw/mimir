"""Contract tests for `mimir.parser.parse_message`.

The parser is the most error-prone code in the project — MIME, RFC
2047, surrogate scrubbing, group addresses, the Python 3.11
AddressHeader bug we worked around, etc. These pin the bits we'd
notice if they regressed.

Inputs are RFC 5322 byte strings constructed inline; nothing read
from disk. Each test names the property it's locking down.
"""
import pytest

from mimir.parser import (
    MAX_RAW_MESSAGE_BYTES,
    MessageTooLarge,
    normalize_subject,
    parse_message,
)


def _msg(extra_headers: bytes = b"", body: bytes = b"hello") -> bytes:
    """Minimum-viable RFC 5322 message; tests pass extra headers and
    a body to override defaults."""
    return (
        b"Message-ID: <abc@example.com>\r\n"
        b"From: A. Person <a@example.com>\r\n"
        b"Subject: hi\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        + extra_headers
        + b"\r\n"
        + body
    )


# Required-field handling


def test_message_id_required():
    raw = b"From: a@b\r\nSubject: x\r\n\r\nbody"
    with pytest.raises(ValueError, match="Message-ID"):
        parse_message(raw)


def test_message_id_strips_angle_brackets():
    art = parse_message(_msg())
    assert art.message_id == "abc@example.com"


def test_in_reply_to_strips_angle_brackets():
    art = parse_message(_msg(extra_headers=b"In-Reply-To: <parent@x.com>\r\n"))
    assert art.in_reply_to == "parent@x.com"


def test_references_split_and_stripped():
    art = parse_message(
        _msg(extra_headers=b"References: <a@x> <b@x>\r\n\t<c@x>\r\n")
    )
    assert art.references == ["a@x", "b@x", "c@x"]


# RFC 2047 encoded-word headers


def test_subject_q_encoded():
    raw = _msg(extra_headers=b"Subject-Override: =?UTF-8?Q?Caf=C3=A9?=\r\n").replace(
        b"Subject: hi", b"Subject: =?UTF-8?Q?Caf=C3=A9?="
    )
    art = parse_message(raw)
    assert art.subject == "Café"


def test_subject_b_encoded():
    raw = _msg().replace(
        b"Subject: hi", b"Subject: =?UTF-8?B?Q2Fmw6k=?="
    )
    art = parse_message(raw)
    assert art.subject == "Café"


def test_from_with_encoded_display_name():
    raw = _msg().replace(
        b"From: A. Person <a@example.com>",
        b"From: =?UTF-8?B?Sm9zw6k=?= <jose@example.com>",
    )
    art = parse_message(raw)
    assert "José" in (art.author or "")


# Surrogate / encoding scrubbing


def test_subject_with_invalid_utf8_does_not_raise():
    """Invalid UTF-8 in the body / headers used to crash older email
    library codepaths; the parser scrubs surrogate codepoints to
    U+FFFD rather than raising."""
    # Construct a Subject byte sequence that, decoded as UTF-8, would
    # contain a lone surrogate.
    raw = _msg().replace(
        b"Subject: hi",
        b"Subject: =?UTF-8?Q?broken=ED=B0=80?=",  # U+DC00 alone
    )
    art = parse_message(raw)
    # No raise; subject is a string. Replacement char OK.
    assert isinstance(art.subject, str)


# Date handling


def test_date_parsed():
    art = parse_message(_msg())
    assert art.date is not None
    assert art.date.year == 2024


def test_malformed_date_does_not_raise():
    raw = _msg().replace(
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000",
        b"Date: garbage that is not a date",
    )
    art = parse_message(raw)
    assert art.date is None


def test_missing_date_returns_none():
    raw = (
        b"Message-ID: <abc@example.com>\r\n"
        b"From: a@b\r\n"
        b"Subject: x\r\n"
        b"\r\nbody"
    )
    art = parse_message(raw)
    assert art.date is None


# Group address handling
# (Python 3.11's email.headerregistry.AddressHeader.parse raises on
# RFC 5322 group addresses; we read From via raw_items() to bypass it.)


def test_group_address_does_not_crash():
    """Group-address From (`"name": a@b, c@d;`) trips the stdlib
    AddressHeader parser; the workaround in `parse_message` reads
    From via `raw_items()` so the raw string survives unchanged.

    Pin the *content* of `art.author` rather than just non-None.
    The raw header text must round-trip: a regression that silently
    normalised, lost, or replaced the group label or the addresses
    would pass an `is not None` check but corrupt downstream display
    (`safe_from`, JSON-LD author.name, atom-feed author)."""
    raw = _msg().replace(
        b"From: A. Person <a@example.com>",
        b'From: "ML": a@b, c@d;',  # group address
    )
    art = parse_message(raw)
    assert art.author is not None
    # The literal characters of the group address must survive.
    # Don't pin exact whitespace -- decode_header normalisation may
    # adjust it -- but pin both the group label and both addresses.
    assert "ML" in art.author
    assert "a@b" in art.author
    assert "c@d" in art.author


# Multipart / attachments


def test_multipart_text_plain_is_body():
    raw = (
        b"Message-ID: <m@x>\r\n"
        b"From: a@b\r\n"
        b"Subject: t\r\n"
        b'Content-Type: multipart/mixed; boundary="bbb"\r\n\r\n'
        b"--bbb\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"hello world\r\n"
        b"--bbb--\r\n"
    )
    art = parse_message(raw)
    assert art.body is not None
    assert "hello world" in art.body


def test_attachment_extracted():
    raw = (
        b"Message-ID: <m@x>\r\n"
        b"From: a@b\r\n"
        b"Subject: t\r\n"
        b'Content-Type: multipart/mixed; boundary="bbb"\r\n\r\n'
        b"--bbb\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"body\r\n"
        b"--bbb\r\n"
        b'Content-Type: application/octet-stream\r\n'
        b'Content-Disposition: attachment; filename="x.bin"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"aGVsbG8=\r\n"
        b"--bbb--\r\n"
    )
    art = parse_message(raw)
    assert len(art.attachments) == 1
    att = art.attachments[0]
    assert att.filename == "x.bin"
    assert att.content_type == "application/octet-stream"
    assert att.content == b"hello"


# Subject normalization


@pytest.mark.parametrize("inp,expected", [
    ("Re: foo", "foo"),
    ("RE: foo", "foo"),
    ("Re: Re: foo", "foo"),
    ("Fwd: bar", "bar"),
    ("Fw: bar", "bar"),
    ("Aw: baz", "baz"),  # German "Antwort"
    ("[PATCH v3] subject", "[patch v3] subject"),  # bracketed tag kept
    ("Re: [PATCH] x", "[patch] x"),
    ("  Re:   spaced  ", "spaced"),
    ("", ""),
    (None, ""),
])
def test_normalize_subject(inp, expected):
    assert normalize_subject(inp) == expected


# Size cap


def test_oversize_input_raises():
    raw = b"Message-ID: <m@x>\r\n\r\n" + b"a" * (MAX_RAW_MESSAGE_BYTES + 1)
    with pytest.raises(MessageTooLarge):
        parse_message(raw)


def test_at_cap_does_not_raise():
    """Exactly the cap is fine; over is the failure."""
    body_size = MAX_RAW_MESSAGE_BYTES - 200  # leave room for headers
    raw = b"Message-ID: <m@x>\r\nFrom: a@b\r\nSubject: t\r\n\r\n" + b"a" * body_size
    assert len(raw) <= MAX_RAW_MESSAGE_BYTES
    art = parse_message(raw)
    assert art.message_id == "m@x"
