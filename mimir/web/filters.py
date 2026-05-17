"""Jinja template filters and the redaction helpers they pivot on.

Two concerns live here: the small filters templates call by name
(`safe_from`, `clean_subject`, `display_name`, `relative_time`,
`is_allowlisted_address`, `is_previewable`, `msg_url`, `render_body`),
and the address-allowlist machinery they all share
(`_is_allowlisted`, `_redact_trailer_address`).

`_is_allowlisted` is memoised on `flask.g` so a long page calling it
50+ times pays the MAINTAINERS-set lookup once. Outside a request
context (CLI render-path tests), it bypasses the memo and goes
straight to the cache.
"""
import re
from datetime import datetime
from email.utils import parseaddr

from flask import g
from pygments.lexers import get_lexer_for_filename, guess_lexer
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

from mimir import maintainer_allowlist
from mimir.config import settings
from mimir.models import Article
from mimir.rendering import render_body
from mimir.web._blueprint import bp_web
from mimir.web.urls import _msg_url, _relative_time


_TEXT_LIKE_EXTENSIONS = {
    ".c", ".h", ".cpp", ".cc", ".hpp", ".rs", ".go",
    ".py", ".sh", ".bash", ".pl", ".rb", ".js", ".ts",
    ".patch", ".diff",
    ".txt", ".md", ".rst", ".cfg", ".ini", ".conf",
    ".yaml", ".yml", ".json", ".xml", ".toml",
    ".s", ".S", ".asm",
    ".dts", ".dtsi",
    ".mk", ".cmake",
}


def _is_previewable(att) -> bool:
    """Heuristic: text-like attachments we can hand to Pygments."""
    ct = (att.content_type or "").lower()
    if ct.startswith("text/"):
        return True
    if ct in {"application/x-patch", "application/x-diff", "application/json", "application/xml"}:
        return True
    if att.filename:
        for ext in _TEXT_LIKE_EXTENSIONS:
            if att.filename.lower().endswith(ext):
                return True
    return False


def _lexer_for(filename: str | None, content: str):
    """Best-effort Pygments lexer choice. Falls back to plain text."""
    if filename:
        try:
            return get_lexer_for_filename(filename, content)
        except ClassNotFound:
            pass
    try:
        return guess_lexer(content)
    except ClassNotFound:
        return TextLexer()


def _is_allowlisted(address: str) -> bool:
    """Union check: static `Settings.email_allowlist` substring tokens
    OR exact membership in the dynamic MAINTAINERS-derived address
    set. Both checks operate on the lowercased address.

    Per-request memoised via `flask.g` so the MAINTAINERS set is
    fetched at most once per render (the underlying DB-backed cache
    is fast, but a long page can call this 50+ times, `g`-caching
    skips the per-call cache lookup entirely after the first hit).

    Outside a request context (CLI, tests calling the filters
    directly without a Flask app), the per-request memo is bypassed
    and the cache layer's own coordination handles repeats.
    """
    addr_lower = address.lower()
    if any(
        token.lower() in addr_lower for token in settings.email_allowlist
    ):
        return True
    try:
        cached = g.setdefault(
            "_maintainer_addresses", maintainer_allowlist.maintainer_addresses(),
        )
    except RuntimeError:
        # Outside a Flask request context; fall through to a direct
        # cache call. Rare; mostly tests.
        cached = maintainer_allowlist.maintainer_addresses()
    return addr_lower in cached


def _redact_trailer_address(email: str) -> str:
    """Return the visible-text replacement for an email on a DCO
    trailer line. Allowlisted addresses survive verbatim so the DCO
    chain stays verifiable for known maintainers; everyone else gets
    `<redacted>`, a placeholder that obviously isn't broken metadata
    (unlike the prior `[off-list ref]` smear from the msgid linkifier
    when it tried to look these up as message-IDs and found nothing).

    Allowlist is the union of static `Settings.email_allowlist` and
    the dynamic MAINTAINERS-derived address set; anyone listed as a
    maintainer or reviewer in the kernel tree's MAINTAINERS file
    surfaces verbatim without operator config.

    Return value is plain text (including the literal angle brackets);
    the trailer renderer html-escapes it before splicing into output.
    """
    if _is_allowlisted(email):
        return f"<{email}>"
    return "<redacted>"


@bp_web.app_template_filter("msg_url")
def _msg_url_filter(article: Article, inbox_name: str) -> str:
    return _msg_url(article, inbox_name)


@bp_web.app_template_filter("render_body")
def _render_body_filter(
    body, msgid_urls=None, parent_url=None, lore_mirror_urls=None,
):
    return render_body(
        body,
        msgid_urls=msgid_urls,
        address_redactor=_redact_trailer_address,
        parent_url=parent_url,
        lore_mirror_urls=lore_mirror_urls,
    )


@bp_web.app_template_filter("is_previewable")
def _is_previewable_filter(att) -> bool:
    return _is_previewable(att)


@bp_web.app_template_filter("safe_from")
def _safe_from_filter(author: str | None) -> str:
    """Return a From-line suitable for display: full address for senders
    in the union of `settings.email_allowlist` (static substring tokens)
    and the MAINTAINERS-derived address set (exact match), otherwise
    display name + `<hidden>` to keep casual senders' addresses out of
    the archive UI."""
    if not author:
        return ""
    name, addr = parseaddr(author)
    if not addr:
        return author  # unparseable; show as-is
    if _is_allowlisted(addr):
        return author
    if name:
        return f"{name} <hidden>"
    return "<hidden>"


_SUBJECT_WS_RE = re.compile(r"\s+")


@bp_web.app_template_filter("clean_subject")
def _clean_subject_filter(subject: str | None) -> str:
    """Collapse RFC 5322 header-folding whitespace into a single space
    for display. Mail headers can carry `\\n + leading spaces` as
    continuation lines; the raw value renders fine but copy-paste and
    link-card previews carry the break verbatim. The parser preserves
    raw fidelity in the DB; display normalises."""
    if not subject:
        return ""
    return _SUBJECT_WS_RE.sub(" ", subject).strip()


@bp_web.app_template_filter("display_name")
def _display_name_filter(author: str | None) -> str:
    """Display name only, for contexts (meta-description, link cards)
    where the `<hidden>` placeholder reads as broken metadata in search
    snippets. Allowlisted senders also surface just their display name
    consistency over leaking addresses into descriptions. Falls back to
    'unknown sender' so the snippet doesn't render with an awkward
    trailing punctuation hole."""
    if not author:
        return "unknown sender"
    name, _ = parseaddr(author)
    if name:
        return name
    return "unknown sender"


def _allowlisted_email(author: str | None) -> str | None:
    """Return the From-line address iff the sender is allowlisted,
    else None. The metadata side of the same allowlist gate
    `_safe_from_filter` uses for visible HTML: structured surfaces
    (JSON-LD `Person.email`, Atom `<author><email>`) mirror what the
    rendered page would have shown, no more and no less. For
    non-allowlisted senders the visible HTML hid the address and
    metadata does the same; for allowlisted senders the address is
    already on the rendered page and in the public git blob, so
    omitting it from metadata under-attributes the only set of
    senders we don't otherwise redact."""
    if not author:
        return None
    _, addr = parseaddr(author)
    if not addr:
        return None
    if _is_allowlisted(addr):
        return addr
    return None


@bp_web.app_template_filter("relative_time")
def _relative_time_filter(then: datetime | None) -> str:
    """Coarse "N{m,h,d} ago" rendering of a tz-aware datetime, with
    a YYYY-MM-DD fallback past 30 days. Thin wrapper around
    `_relative_time` so templates can spell it as a Jinja filter:
    `{{ thing.last_activity|relative_time }}`."""
    if then is None:
        return ""
    return _relative_time(then)


@bp_web.app_template_filter("is_allowlisted_address")
def _is_allowlisted_address_filter(address: str | None) -> bool:
    """True iff `address` is in the allowlist (static tokens
    OR MAINTAINERS-derived set).

    Used by templates to decide whether to render a clickable
    reviewer link. The reviewer page itself (`/<inbox>/reviewer/<addr>`)
    accepts any address, but mimir only generates outbound links for
    allowlisted addresses, this keeps non-public addresses out of
    URL bars / browser history / scraper paths reached via mimir's
    own navigation, matching the redaction posture of `safe_from`.
    """
    if not address:
        return False
    return _is_allowlisted(address)
