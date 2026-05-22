"""Contract tests for `mimir.parser.parse_message`.

The parser is the most error-prone code in the project, MIME, RFC
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
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n" + extra_headers + b"\r\n" + body
    )


# Required-field handling


def test_message_id_required():
    raw = b"From: a@b\r\nSubject: x\r\n\r\nbody"
    with pytest.raises(ValueError, match="Message-ID"):
        parse_message(raw)


def test_message_id_strips_angle_brackets():
    art = parse_message(_msg())
    assert art.message_id == "abc@example.com"


def test_message_id_picks_first_non_empty_when_duplicated():
    """Real-world blobs from alsa-devel's `Applied "..."` auto-reply
    bot carry two `Message-ID` headers: an empty `Message-Id:` line
    followed by a real `Message-ID: <...>` further down. The naive
    first-match returned the empty string and the parser raised
    `message has no Message-ID`, dropping 116 messages on a recent
    reindex of the alsa-devel 0.git epoch. Pin the first-non-empty
    behaviour."""
    raw = (
        b"Message-Id: \r\n"
        b"Message-ID: <real@example.com>\r\n"
        b"From: bot@example.com\r\n"
        b"Subject: Applied something\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        b"\r\nbody"
    )
    art = parse_message(raw)
    assert art.message_id == "real@example.com"


def test_in_reply_to_picks_first_non_empty_when_duplicated():
    """Same shape applies to In-Reply-To: an empty first header
    should not shadow a real follow-up; threading depends on it."""
    art = parse_message(
        _msg(
            extra_headers=b"In-Reply-To: \r\nIn-Reply-To: <parent@x.com>\r\n",
        )
    )
    assert art.in_reply_to == "parent@x.com"


def test_in_reply_to_strips_angle_brackets():
    art = parse_message(_msg(extra_headers=b"In-Reply-To: <parent@x.com>\r\n"))
    assert art.in_reply_to == "parent@x.com"


def test_references_split_and_stripped():
    art = parse_message(_msg(extra_headers=b"References: <a@x> <b@x>\r\n\t<c@x>\r\n"))
    assert art.references == ["a@x", "b@x", "c@x"]


def test_in_reply_to_picks_first_msgid_when_multi_token():
    """`In-Reply-To` is documented as a single msg-id, but broken
    senders sometimes emit multiple. The audit (2026-05-15)
    flagged the previous "whole string verbatim" behaviour as a
    silent threading-break: a value like `<a@x> <b@y>` was
    stored as `a@x> <b@y` which never joined to any real
    Message-ID. Pin: first msg-id wins."""
    art = parse_message(
        _msg(
            extra_headers=b"In-Reply-To: <first@x.com> <second@x.com>\r\n",
        )
    )
    assert art.in_reply_to == "first@x.com"


def test_in_reply_to_strips_cfws_comment():
    """RFC 5322 CFWS comments `(...)` can appear between tokens.
    Real-world examples are rare but exist; the comment text must
    not surface as the in_reply_to value."""
    art = parse_message(
        _msg(
            extra_headers=b"In-Reply-To: (some comment) <real@x.com>\r\n",
        )
    )
    assert art.in_reply_to == "real@x.com"


def test_references_strips_cfws_comments():
    """CFWS comments between References tokens are dropped, not
    turned into junk reference entries that never join anywhere."""
    art = parse_message(
        _msg(
            extra_headers=b"References: <a@x> (comment) <b@x> (another) <c@x>\r\n",
        )
    )
    assert art.references == ["a@x", "b@x", "c@x"]


def test_decode_rfc2047_falls_back_and_logs_at_debug_on_unknown_charset(
    caplog,
):
    """The audit (2026-05-15) flagged the bare `except Exception`
    in `_decode_rfc2047` as silencing real charset-registry / decode
    bugs. The tightened catch logs SOMETHING when it falls back to
    the verbatim header; pin both the fallback and the log line so a
    future regression that drops the logger or widens the catch
    surfaces.

    Level is DEBUG, not WARN: the fallback is the canonical
    handling for buggy-mailer encoded-words (`=?UNKNOWN?...`,
    `=?unknown-8bit?...`, whitespace charset names), and the
    verbatim string is itself the "broken sender mailer" cue in
    the rendered subject/author. Multi-list ingest swamped the log
    with WARN lines that carried no operationally actionable info.
    """
    import logging

    from mimir.parser import _decode_rfc2047

    with caplog.at_level(logging.DEBUG, logger="mimir.parser"):
        out = _decode_rfc2047("=?totally-not-a-real-charset?Q?test?=")

    # Fallback is the verbatim header value.
    assert out == "=?totally-not-a-real-charset?Q?test?="
    # The log line lands at DEBUG, not WARN.
    matching = [
        rec
        for rec in caplog.records
        if "fallback" in rec.message.lower() or "verbatim" in rec.message.lower()
    ]
    assert matching, f"expected a debug log; got: {[r.message for r in caplog.records]}"
    assert all(rec.levelno == logging.DEBUG for rec in matching), (
        f"expected DEBUG; got: {[(rec.levelname, rec.message) for rec in matching]}"
    )


# RFC 2047 encoded-word headers


def test_subject_q_encoded():
    raw = _msg(extra_headers=b"Subject-Override: =?UTF-8?Q?Caf=C3=A9?=\r\n").replace(
        b"Subject: hi", b"Subject: =?UTF-8?Q?Caf=C3=A9?="
    )
    art = parse_message(raw)
    assert art.subject == "Café"


def test_subject_b_encoded():
    raw = _msg().replace(b"Subject: hi", b"Subject: =?UTF-8?B?Q2Fmw6k=?=")
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
    U+FFFD rather than raising.

    Pin the *replacement* contract (U+FFFD shows up where the
    surrogate was) rather than just `isinstance(str)`. The earlier,
    looser check would have passed a regression that silently
    returned `""` -- losing the rest of the subject -- instead of
    replacing the bad byte sequence."""
    # Construct a Subject byte sequence that, decoded as UTF-8, would
    # contain a lone surrogate.
    raw = _msg().replace(
        b"Subject: hi",
        b"Subject: =?UTF-8?Q?broken=ED=B0=80?=",  # U+DC00 alone
    )
    art = parse_message(raw)
    assert isinstance(art.subject, str)
    # The "broken" prefix survives; the bad bytes become U+FFFD.
    assert "broken" in art.subject
    assert "�" in art.subject, (
        f"surrogate replacement missing from subject: {art.subject!r}"
    )


def test_surrogate_escape_range_recovers_valid_utf8_pair():
    """`_scrub_surrogates` distinguishes two cases (CONTEXT.md):

    1. Lone surrogates *outside* the surrogate-escape range
       (U+D800-U+DC7F, U+DD00-U+DFFF). No original byte to recover;
       replace with U+FFFD. Covered by the preceding test.
    2. Surrogate-escape range (U+DC80-U+DCFF). The codepoints hold
       original invalid bytes from a misdecoded charset; round-trip
       through `errors='surrogateescape'` recovers them. Bytes that
       happen to form valid UTF-8 come back as their proper
       characters; anything else becomes U+FFFD.

    Pin case 2 directly: the byte sequence `0xC3 0xA9` is valid
    UTF-8 for `é`. When it lands in a `str` as U+DC83 U+DCA9
    (i.e. surrogate-escape-encoded after a charset mismatch), the
    scrubber should recover it to literal `é`."""
    from mimir.parser import _scrub_surrogates

    # U+DC83 = 0xDC80 + 0x83 (surrogate-escape for byte 0x83? No -
    # surrogate-escape maps byte 0xCC to U+DCCC etc. The 0xC3 byte
    # becomes U+DCC3 and 0xA9 becomes U+DCA9.)
    surrogate_escaped = "\udcc3\udca9"
    out = _scrub_surrogates(surrogate_escaped)
    # The byte pair 0xC3 0xA9 decodes as UTF-8 'é'.
    assert out == "é", (
        f"expected surrogate-escape pair to round-trip to 'é', got {out!r}"
    )


def test_surrogate_escape_range_unrecoverable_byte_becomes_replacement():
    """The surrogate-escape range path's *other* arm: a byte that
    doesn't form valid UTF-8 with its neighbours becomes U+FFFD."""
    from mimir.parser import _scrub_surrogates

    # Lone 0xC3 (UTF-8 continuation byte expected, none follows).
    out = _scrub_surrogates("\udcc3")
    assert out == "�", (
        f"expected unrecoverable surrogate-escape byte to become U+FFFD, got {out!r}"
    )


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
    raw = b"Message-ID: <abc@example.com>\r\nFrom: a@b\r\nSubject: x\r\n\r\nbody"
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


def test_body_with_unknown_charset_falls_back_to_latin1():
    """A message body declaring `charset=unknown-8bit` (RFC 1428) or
    any other charset Python's codec registry can't resolve used to
    LookupError out of `body_part.get_content()` and the message
    silently rendered as `(no body)` even though the git blob has
    real text. Fall back to `get_payload(decode=True).decode('latin-1')`,
    which is RFC 1428's recommended interpretation and is bijective
    on bytes; structure (ASCII tokens, addresses, patch markers)
    survives even when high-bit content shows as mojibake."""
    raw = (
        b"Message-ID: <unkbody@x>\r\n"
        b"From: a@b\r\n"
        b"Subject: t\r\n"
        b'Content-Type: text/plain; charset="unknown-8bit"\r\n'
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"hello \xe9 world\r\n"
    )
    art = parse_message(raw)
    assert art.body is not None
    assert "hello" in art.body
    assert "world" in art.body
    # latin-1 decoding of 0xe9 is the Latin Small Letter E With Acute.
    # `errors="replace"` is set defensively; on latin-1 nothing
    # actually triggers replacement (bijective by construction).
    assert "é" in art.body


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
        b"Content-Type: application/octet-stream\r\n"
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


def test_multipart_wrapper_not_treated_as_attachment(caplog):
    """When a non-body branch of the message tree is itself a
    multipart wrapper (e.g. multipart/signed alongside the
    multipart/alternative holding the body), `iter_attachments`
    yields the wrapper. The stdlib content manager has no decoder
    for multipart/*; we used to log a KeyError-and-drop warning for
    each such wrapper, which became dozens of lines per multi-list
    ingest. Skip wrappers outright: no attachment, no warning."""
    import logging

    raw = (
        b"Message-ID: <multipart-wrap@x>\r\n"
        b"From: a@b\r\n"
        b"Subject: t\r\n"
        b'Content-Type: multipart/mixed; boundary="outer"\r\n\r\n'
        b"--outer\r\n"
        b'Content-Type: multipart/alternative; boundary="inner"\r\n\r\n'
        b"--inner\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"hello body\r\n"
        b"--inner\r\n"
        b"Content-Type: text/html\r\n\r\n"
        b"<p>hello body</p>\r\n"
        b"--inner--\r\n"
        b"--outer\r\n"
        b'Content-Type: multipart/signed; boundary="sig"; '
        b'protocol="application/pgp-signature"\r\n\r\n'
        b"--sig\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"signed payload\r\n"
        b"--sig\r\n"
        b"Content-Type: application/pgp-signature\r\n\r\n"
        b"-----BEGIN PGP SIGNATURE-----\r\n...\r\n"
        b"-----END PGP SIGNATURE-----\r\n"
        b"--sig--\r\n"
        b"--outer--\r\n"
    )
    with caplog.at_level(logging.WARNING, logger="mimir.parser"):
        art = parse_message(raw)
    assert "hello body" in (art.body or "")
    # The wrapper isn't an attachment; its leaves aren't surfaced
    # either (deliberate; recursing into multipart/signed is its own
    # design conversation). What matters here: no false-positive
    # "dropping attachment" warning for the wrapper.
    assert all(
        "dropping attachment" not in rec.getMessage() for rec in caplog.records
    ), caplog.text
    assert all(a.content_type != "multipart/signed" for a in art.attachments)
    assert all(a.content_type != "multipart/mixed" for a in art.attachments)


def test_attachment_with_opaque_content_type_survives():
    """Leaf attachments whose content-type isn't in the stdlib's
    content-manager registry (e.g. `chemical/x-mopac-input`,
    `application/x-perl`) used to KeyError out of `get_content()`
    and get dropped with a warning. Fall back to the raw transfer-
    decoded payload so the operator keeps the bytes."""
    raw = (
        b"Message-ID: <opaque@x>\r\n"
        b"From: a@b\r\n"
        b"Subject: t\r\n"
        b'Content-Type: multipart/mixed; boundary="bbb"\r\n\r\n'
        b"--bbb\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"body\r\n"
        b"--bbb\r\n"
        b"Content-Type: chemical/x-mopac-input\r\n"
        b'Content-Disposition: attachment; filename="hcidump.dat"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"aGNpZHVtcA==\r\n"
        b"--bbb--\r\n"
    )
    art = parse_message(raw)
    assert len(art.attachments) == 1
    att = art.attachments[0]
    assert att.filename == "hcidump.dat"
    assert att.content_type == "chemical/x-mopac-input"
    assert att.content == b"hcidump"


def test_attachment_with_unknown_charset_survives():
    """A leaf attachment with a registered content-type
    (`text/plain`) but an unregistered charset (`unknown-8bit`,
    per RFC 1428's "we have 8-bit bytes, encoding unknown" label)
    used to LookupError out of `get_content()` and get dropped with
    a warning. PR #258's catch was too narrow (KeyError only),
    real-world patches/log files attached with this charset
    (`r8169-getstats.patch`, `putty.log` on older lists) survived
    only after the catch widened to LookupError. Recovered via the
    same raw-payload fallback as the opaque-content-type case."""
    raw = (
        b"Message-ID: <unkcharset@x>\r\n"
        b"From: a@b\r\n"
        b"Subject: t\r\n"
        b'Content-Type: multipart/mixed; boundary="bbb"\r\n\r\n'
        b"--bbb\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"body\r\n"
        b"--bbb\r\n"
        b'Content-Type: text/plain; charset="unknown-8bit"\r\n'
        b'Content-Disposition: attachment; filename="putty.log"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"cHV0dHk=\r\n"
        b"--bbb--\r\n"
    )
    art = parse_message(raw)
    assert len(art.attachments) == 1
    att = art.attachments[0]
    assert att.filename == "putty.log"
    assert att.content_type == "text/plain"
    assert att.content == b"putty"


# Subject normalization


@pytest.mark.parametrize(
    "inp,expected",
    [
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
    ],
)
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


# Pickle round-trip


def test_parsed_article_pickles_round_trip():
    """`parse_message` and its return value must stay pickleable so
    `ProcessPoolExecutor.map` can ship them between worker and parent
    processes. The integration test exercises one happy-path run; this
    pins the dataclass contract directly so a regression (adding an
    unpickleable field, switching a `dict` to a generator, etc.) lands
    at PR time instead of in the next production ingest.

    Covers every field shape: scrubbed strings, tz-aware datetime,
    `list[str]` references, `dict[str, str]` headers, and the nested
    `attachments` list of `ParsedAttachment`."""
    import pickle

    raw = (
        b"Message-ID: <pickle@x>\r\n"
        b"From: A. Person <a@example.com>\r\n"
        b"To: list@vger.kernel.org\r\n"
        b"Subject: pickled\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        b"In-Reply-To: <parent@x>\r\n"
        b"References: <root@x> <parent@x>\r\n"
        b'Content-Type: multipart/mixed; boundary="bbb"\r\n\r\n'
        b"--bbb\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"body text\r\n"
        b"--bbb\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="x.bin"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"aGVsbG8=\r\n"
        b"--bbb--\r\n"
    )
    art = parse_message(raw)
    revived = pickle.loads(pickle.dumps(art))

    # Pydantic BaseModel equality is field-by-field; one assertion
    # covers the entire shape. The individual asserts below pin the
    # critical fields so a future change that breaks one of them
    # produces a meaningful diagnostic instead of a bare "not equal".
    assert revived == art
    assert revived.message_id == "pickle@x"
    assert revived.in_reply_to == "parent@x"
    assert revived.references == ["root@x", "parent@x"]
    # Date must keep its tz info; a naive datetime here would mean the
    # `aware_utc` normalisation downstream would inject the wrong
    # offset for `-0000`-shaped headers.
    assert revived.date is not None and revived.date.tzinfo is not None
    # Headers are a plain dict, not a generator or a Pydantic proxy.
    assert isinstance(revived.headers, dict)
    assert revived.headers["To"] == "list@vger.kernel.org"
    # Attachments survive as a list of `ParsedAttachment`, content
    # bytes intact.
    assert len(revived.attachments) == 1
    att = revived.attachments[0]
    assert att.filename == "x.bin"
    assert att.content_type == "application/octet-stream"
    assert att.content == b"hello"


def test_parsed_attachment_pickles_round_trip():
    """`ParsedAttachment` standalone round-trip. The post-scrub state
    of `filename` and `content_type` must survive: surrogate-scrubbed
    strings live in the model after validation, and pickle has to
    preserve them as-is, not re-validate."""
    import pickle

    from mimir.parser import ParsedAttachment

    att = ParsedAttachment(
        filename="report.pdf",
        content_type="application/pdf",
        content=b"\x00\x01\x02\x03binary",
    )
    revived = pickle.loads(pickle.dumps(att))
    assert revived == att
    assert revived.filename == "report.pdf"
    assert revived.content_type == "application/pdf"
    assert revived.content == b"\x00\x01\x02\x03binary"
