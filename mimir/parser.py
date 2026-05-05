import re
from datetime import datetime
from email import policy
from email.header import decode_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

from pydantic import BaseModel, Field, field_validator


# Keep only the headers we actually use for display, threading, and triage.
# The big space-eaters in lore archives — Received chains, DKIM/ARC blobs,
# X-Spam-*, Authentication-Results — are all dropped. Compared with raw,
# this typically cuts the headers dict by 50-70%.
KEPT_HEADERS = frozenset({
    "subject", "from", "to", "cc", "bcc", "reply-to", "sender",
    "date", "message-id", "in-reply-to", "references", "newsgroups",
    "content-type", "content-transfer-encoding", "mime-version",
    "list-id", "list-archive", "list-help", "list-post",
    "list-unsubscribe", "list-subscribe",
    "user-agent", "x-mailer", "organization", "archived-at",
})


_NON_ESCAPE_SURROGATE_RE = re.compile(r"[\ud800-\udc7f\udd00-\udfff]")


def _scrub_surrogates(s: str) -> str:
    """Strip codepoints that can't survive a UTF-8 round-trip.

    Two layers:
    1. Lone surrogates outside the surrogate-escape range (e.g. U+DF9D from
       broken UTF-16) have no original byte to recover. Replace with U+FFFD.
    2. Surrogate-escape codepoints (U+DC80–U+DCFF) hold the original invalid
       bytes from a misdecoded charset. Round-tripping through bytes recovers
       them — pairs that happened to be valid UTF-8 (e.g. 0xC3 0xA9) come
       back as their proper characters; anything else becomes U+FFFD.
    """
    s = _NON_ESCAPE_SURROGATE_RE.sub("�", s)
    return s.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")


class ParsedAttachment(BaseModel):
    filename: str | None = None
    content_type: str
    content: bytes

    @field_validator("filename", "content_type", mode="after")
    @classmethod
    def _scrub_str(cls, v: str | None) -> str | None:
        return _scrub_surrogates(v) if v is not None else v


class ParsedArticle(BaseModel):
    message_id: str
    subject: str | None = None
    author: str | None = None
    date: datetime | None = None
    body: str | None = None
    body_content_type: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    attachments: list[ParsedAttachment] = Field(default_factory=list)

    @field_validator(
        "message_id", "subject", "author", "body",
        "body_content_type", "in_reply_to",
        mode="after",
    )
    @classmethod
    def _scrub_str(cls, v: str | None) -> str | None:
        return _scrub_surrogates(v) if v is not None else v

    @field_validator("references", mode="after")
    @classmethod
    def _scrub_references(cls, v: list[str]) -> list[str]:
        return [_scrub_surrogates(s) for s in v]

    @field_validator("headers", mode="after")
    @classmethod
    def _scrub_headers(cls, v: dict[str, str]) -> dict[str, str]:
        return {k: _scrub_surrogates(val) for k, val in v.items()}


def _normalize_msgid(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lstrip("<").rstrip(">").strip() or None


_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:re|fwd?|aw|sv|antw|antwort|wg|tr)\s*:\s*",
    re.IGNORECASE,
)


def normalize_subject(value: str | None) -> str:
    """Strip leading reply/forward prefixes ("Re:", "Fwd:", "Aw:", ...) so
    sibling orphan threads with the same conversation subject can be
    matched. Bracketed tags like "[PATCH v3]" are *kept* — they
    distinguish patch revisions, which we want to treat as related-but-
    distinct threads. Returns lowercase, whitespace-collapsed."""
    if not value:
        return ""
    s = value
    while True:
        new = _SUBJECT_PREFIX_RE.sub("", s, count=1)
        if new == s:
            break
        s = new
    return " ".join(s.split()).lower()


def _split_references(value: str | None) -> list[str]:
    if not value:
        return []
    return [r.strip().lstrip("<").rstrip(">") for r in value.split() if r.strip()]


def _decode_rfc2047(value: str | None) -> str | None:
    """Decode RFC 2047 encoded-words. Safe on plain ASCII strings (returns as-is)."""
    if value is None:
        return None
    try:
        chunks = []
        for chunk, charset in decode_header(value):
            if isinstance(chunk, bytes):
                chunks.append(chunk.decode(charset or "ascii", errors="replace"))
            else:
                chunks.append(chunk)
        return "".join(chunks)
    except Exception:
        return value


def _raw_header(msg: EmailMessage, name: str) -> str | None:
    """Get a header's source string via msg.raw_items, bypassing policy parsing.

    We avoid msg[name] for everything except structural body/attachment work
    because email.headerregistry.AddressHeader.parse in Python 3.11 raises
    AttributeError on RFC 5322 group addresses (Group has no .local_part).
    """
    target = name.lower()
    for k, v in msg.raw_items():
        if k.lower() == target:
            return v
    return None


def _attachment_bytes(part) -> bytes:
    content = part.get_content()
    if isinstance(content, EmailMessage):
        return content.as_bytes()
    if isinstance(content, str):
        return content.encode(part.get_content_charset() or "utf-8", errors="replace")
    return content


class MessageTooLarge(ValueError):
    """Raised when `parse_message` is handed a blob bigger than
    `MAX_RAW_MESSAGE_BYTES`. Caught by the ingest worker and
    counted as `failed` so the offending commit is recorded but
    doesn't take the parser pool down."""


# Hard ceiling on the input bytes `parse_message` will accept. lkml
# attachments occasionally cross 10 MB (full vmlinux, dmesg dumps);
# 50 MB is comfortably above the 99.99th-percentile message size and
# bounds worker memory at ~workers × 50 MB even in the worst case.
MAX_RAW_MESSAGE_BYTES = 50 * 1024 * 1024


def parse_message(raw: bytes) -> ParsedArticle:
    if len(raw) > MAX_RAW_MESSAGE_BYTES:
        raise MessageTooLarge(
            f"raw message is {len(raw)} bytes; cap is {MAX_RAW_MESSAGE_BYTES}"
        )
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)

    message_id = _normalize_msgid(_raw_header(msg, "Message-ID"))
    if not message_id:
        raise ValueError("message has no Message-ID")

    body_part = msg.get_body(preferencelist=("plain", "html"))
    body: str | None = None
    body_content_type: str | None = None
    if body_part is not None:
        try:
            body = body_part.get_content()
        except (LookupError, UnicodeDecodeError):
            body = None
        body_content_type = body_part.get_content_type()

    attachments: list[ParsedAttachment] = []
    for part in msg.iter_attachments():
        try:
            attachments.append(
                ParsedAttachment(
                    filename=part.get_filename(),
                    content_type=part.get_content_type(),
                    content=_attachment_bytes(part),
                )
            )
        except Exception:
            continue

    date: datetime | None = None
    raw_date = _raw_header(msg, "Date")
    if raw_date:
        try:
            date = parsedate_to_datetime(raw_date)
        except (TypeError, ValueError):
            date = None

    headers: dict[str, str] = {}
    for k, v in msg.raw_items():
        if v is not None and k.lower() in KEPT_HEADERS:
            headers[k] = v

    return ParsedArticle(
        message_id=message_id,
        subject=_decode_rfc2047(_raw_header(msg, "Subject")),
        author=_decode_rfc2047(_raw_header(msg, "From")),
        date=date,
        body=body,
        body_content_type=body_content_type,
        in_reply_to=_normalize_msgid(_raw_header(msg, "In-Reply-To")),
        references=_split_references(_raw_header(msg, "References")),
        headers=headers,
        attachments=attachments,
    )
