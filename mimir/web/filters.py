"""Jinja template filters and the redaction helpers they pivot on.

Two concerns live here: the small filters templates call by name
(`safe_from`, `clean_subject`, `display_name`, `relative_time`,
`is_allowlisted_address`, `is_previewable`, `msg_url`, `render_body`),
and the address-allowlist machinery they all share
(`_is_allowlisted`, `_redact_trailer_address`). The display-shaped
helpers feeding the filters (`_relative_time`, `_thread_summary`)
also live here, since their natural home is "what templates render"
rather than "URL composition" where they used to sit.

`_is_allowlisted` is memoised on `flask.g` so a long page calling it
50+ times pays the MAINTAINERS-set lookup once. Outside a request
context (CLI render-path tests), it bypasses the memo and goes
straight to the cache.
"""

import re
from datetime import UTC, datetime
from email.utils import parseaddr

from flask import g
from pygments.lexers import get_lexer_for_filename, guess_lexer
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

from mimir import maintainer_allowlist
from mimir.config import settings
from mimir.datetime_utils import aware_utc
from mimir.maintainer_directory import (
    is_addressable_maintainer_address,
    maintainer_path,
)
from mimir.rendering import render_body, render_body_text
from mimir.rendering.linkify import EMAIL_TOKEN_RE as _EMAIL_TOKEN_RE
from mimir.subsystems import (
    is_addressable_subsystem_name,
    subsystem_index_path,
    subsystem_path,
)
from mimir.trailers import REVIEW_TRAILER_ROLES
from mimir.web._blueprint import bp_web
from mimir.web.urls import (
    _msg_path,
    _msg_url,
    _thread_view_url,
    attachment_path,
    is_addressable_reviewer_address,
    markdown_path,
)

bp_web.add_app_template_filter(
    is_addressable_reviewer_address, "is_addressable_reviewer"
)


def _relative_time(then: datetime, now: datetime | None = None) -> str:
    """Render a coarse relative-time string for the closed-state fold
    summary ("23 messages, 5 authors, 2h ago"). Uses minutes/hours/days
    units under 30 days; falls back to an absolute YYYY-MM-DD beyond
    that, since "47d ago" is harder to parse than the date itself."""
    if now is None:
        now = datetime.now(UTC)
    then = aware_utc(then)
    delta = now - then
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 86400 * 30:
        return f"{secs // 86400}d ago"
    return then.strftime("%Y-%m-%d")


def thread_summary_from(authors, last_activity, total: int) -> dict:
    """The headline stats, from pre-aggregated pieces.

    Split out so the paginated thread view can pass thread-WIDE values
    it fetched separately, while the message page keeps passing its
    in-memory node list. One implementation of the tally either way,
    which matters because it is not a plain distinct count: authors are
    reduced by `parseaddr` so display-name drift ("Alice <a@x>" vs
    "alice <a@x>") counts once. A SQL `COUNT(DISTINCT author)` would
    quietly report two people there.

    `total` is the fallback when no address parses at all, preserving
    the original behaviour of showing the message count rather than 0.
    """
    emails: set[str] = set()
    for author in authors:
        if not author:
            continue
        _, addr = parseaddr(author)
        if addr:
            emails.add(addr.lower())
    return {
        "author_count": len(emails) or total,
        "last_activity_rel": _relative_time(last_activity) if last_activity else "?",
    }


def _thread_summary(thread) -> dict:
    """Headline stats for an in-memory thread (the message page).

    Thin wrapper over `thread_summary_from`; the thread view uses that
    directly because its page is a slice and cannot answer thread-wide
    questions from what it rendered.
    """
    if not thread:
        return {"author_count": 0, "last_activity_rel": "?"}
    dates = [n.date for n in thread if n.date]
    return thread_summary_from(
        (n.author for n in thread),
        max(dates) if dates else None,
        len(thread),
    )


_TEXT_LIKE_EXTENSIONS = {
    ".c",
    ".h",
    ".cpp",
    ".cc",
    ".hpp",
    ".rs",
    ".go",
    ".py",
    ".sh",
    ".bash",
    ".pl",
    ".rb",
    ".js",
    ".ts",
    ".patch",
    ".diff",
    ".txt",
    ".md",
    ".rst",
    ".cfg",
    ".ini",
    ".conf",
    ".yaml",
    ".yml",
    ".json",
    ".xml",
    ".toml",
    ".s",
    ".S",
    ".asm",
    ".dts",
    ".dtsi",
    ".mk",
    ".cmake",
}


def _is_previewable(att) -> bool:
    """Heuristic: text-like attachments we can hand to Pygments."""
    ct = (att.content_type or "").lower()
    if ct.startswith("text/"):
        return True
    if ct in {
        "application/x-patch",
        "application/x-diff",
        "application/json",
        "application/xml",
    }:
        return True
    if att.filename:
        return att.filename.lower().endswith(tuple(_TEXT_LIKE_EXTENSIONS))
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


def _token_matches(token: str, address: str) -> bool:
    """One `email_allowlist` token against a lowercased address, by
    structure (#863): `local@` is that local part at any domain,
    `local@domain` is the exact address, and `@domain` or a bare
    `domain` is exactly that domain, subdomains not included. A
    substring match let `a@kernel.org.evil.example` through."""
    local, at, domain = address.partition("@")
    if not at or "@" in domain:
        return False
    token = token.lower()
    if token.endswith("@"):
        return local == token[:-1]
    if "@" in token.lstrip("@"):
        return address == token
    return domain == token.lstrip("@")


def _is_allowlisted(address: str) -> bool:
    """Union check: static `Settings.email_allowlist` tokens (matched
    by address structure, see `_token_matches`) OR exact membership in
    the dynamic MAINTAINERS-derived address set. Both checks operate
    on the lowercased address.

    Per-request memoised via `flask.g` so the MAINTAINERS set is
    fetched at most once per render (the underlying DB-backed cache
    is fast, but a long page can call this 50+ times, `g`-caching
    skips the per-call cache lookup entirely after the first hit).

    Outside a request context (CLI, tests calling the filters
    directly without a Flask app), the per-request memo is bypassed
    and the cache layer's own coordination handles repeats.
    """
    addr_lower = address.lower()
    if any(_token_matches(token, addr_lower) for token in settings.email_allowlist):
        return True
    try:
        # Not `g.setdefault(key, maintainer_addresses())`: the default is
        # evaluated before the call, so that read the set on every call.
        if "_maintainer_addresses" not in g:
            g._maintainer_addresses = maintainer_allowlist.maintainer_addresses()
        cached = g._maintainer_addresses
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


bp_web.add_app_template_filter(_msg_url, "msg_url")
# `msg_path(article_id, date, inbox_name)` for templates that hold rows
# rather than Articles; the same function as `msg_url`, so no template
# spells a message path by hand (#768).
bp_web.add_app_template_global(_msg_path, "msg_path")
bp_web.add_app_template_filter(_is_previewable, "is_previewable")

# `thread_url`: listings link the subject to the message page and the
# reply count to the whole-thread view, so the thread view gains
# inbound links from the pages that actually get crawled. It is the
# canonical target for every message in a multi-message thread and the
# URL the sitemap lists, yet before this filter the entire template set
# contained exactly ONE link to it (`message.html`, conditional), while
# ten templates linked message pages. A canonical target reachable only
# through the pages that disclaim themselves is a weak structure,
# whatever the canonical tag says.
#
# Callers must gate on the thread actually having replies; `/t` on a
# single-message thread is a poorer page than the message itself and
# deliberately not its canonical.
bp_web.add_app_template_filter(_thread_view_url, "thread_url")


@bp_web.app_template_filter("render_body")
def _render_body_filter(
    body,
    msgid_urls=None,
    parent_url=None,
    lore_mirror_urls=None,
    compact=False,
    text_more=None,
):
    return render_body(
        body,
        msgid_urls=msgid_urls,
        address_redactor=_redact_trailer_address,
        parent_url=parent_url,
        lore_mirror_urls=lore_mirror_urls,
        compact=compact,
        text_more=text_more,
    )


@bp_web.app_template_filter("render_body_text")
def _render_body_text_filter(body) -> str:
    """The Markdown pages' body: `render_body`'s filtering with the
    same redactor, as plain text (#884)."""
    return render_body_text(body, address_redactor=_redact_trailer_address)


bp_web.add_app_template_filter(markdown_path, "markdown_path")
bp_web.add_app_template_global(attachment_path, "attachment_path")

_BACKTICK_RUN_RE = re.compile(r"`+")


@bp_web.app_template_filter("md_fence")
def _md_fence(text: str) -> str:
    """A code fence longer than any backtick run in `text`, so no line
    of a body can close it early (patches to `.md` and `.rst` docs
    carry triple backticks). One linear scan."""
    longest = max((len(run) for run in _BACKTICK_RUN_RE.findall(text)), default=0)
    return "`" * max(3, longest + 1)


_MD_SPECIAL_RE = re.compile(r"([\\`*_\[\]<>#|~&])")


@bp_web.app_template_filter("md_escape")
def _md_escape(text: str | None) -> str:
    """Sender-controlled text (a subject, a name, a filename) as inert
    Markdown on one line: whitespace collapsed, and the characters that
    open emphasis, links, code, HTML, headings or entity references
    backslash-escaped. A bare `<hidden>` would otherwise be read as an
    HTML tag and vanish, and `a&commat;b&period;org` decoded into an
    address that the HTML page shows as literal text."""
    return _MD_SPECIAL_RE.sub(r"\\\1", _SUBJECT_WS_RE.sub(" ", text or "").strip())


@bp_web.app_template_filter("safe_from")
def _safe_from_filter(author: str | None) -> str:
    """Return a From-line suitable for display: full address for senders
    in the union of `settings.email_allowlist` (static tokens)
    and the MAINTAINERS-derived address set (exact match), otherwise
    display name + `<hidden>` to keep casual senders' addresses out of
    the archive UI."""
    if not author:
        return ""
    _, addr = parseaddr(author)
    if not addr:
        # Unparseable: as-is unless it holds an address we would hide.
        if any(not _is_allowlisted(t) for t in _EMAIL_TOKEN_RE.findall(author)):
            return "<hidden>"
        return author
    usable = _usable_display_name(author)
    if _is_allowlisted(addr):
        # Rebuilt, never `author`: a trailing "(comment)" can hold an address.
        return f"{usable} <{addr}>" if usable else addr
    if usable:
        return f"{usable} <hidden>"
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
    # An allowlisted address is already public, and JSON-LD and Atom
    # name a nameless allowlisted sender by it (#845, #850).
    return (
        _usable_display_name(author) or _allowlisted_email(author) or "unknown sender"
    )


def _usable_name(name: str | None) -> str | None:
    """A display name, or None when it is empty or holds an
    email-shaped token that is not allowlisted (#852). `"a@x.org"
    <a@x.org>` and a list rewrite like `"a@x.org via list" <list@y.org>`
    would otherwise print the address through the name. Errs towards
    hiding: a false positive drops a name, a false negative leaks an
    address. "John @ Company" is not email-shaped and stays. The one
    rule for From names and trailer names (#902)."""
    name = (name or "").strip()
    if not name:
        return None
    if any(not _is_allowlisted(m) for m in _EMAIL_TOKEN_RE.findall(name)):
        return None
    return name


def _usable_display_name(author: str | None) -> str | None:
    """The From display name through `_usable_name`."""
    return _usable_name(parseaddr(author or "")[0])


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


@bp_web.app_template_filter("patch_synthesis")
def _patch_synthesis_filter(patch_state) -> str:
    """One-sentence, human-readable summary of a patch's lifecycle,
    composed from data the state card already carries.

    Exists for SEO index-shaping (design doc 2026-07-27, W3b): the
    badges and the state card encode this same information as glyphs
    and pills, which read well for a human scanning but give a crawler
    nothing indexable. Rendering it once as prose gives the page
    unique, query-matching text that no other LKML mirror emits,
    without changing what the badges do. Where the patch landed is the
    status line's (`status_line`), read from the pill's own row (#844).

    Returns "" for non-patch articles and for patches with nothing
    worth saying, so the template can render it unconditionally.
    """
    if patch_state is None or not patch_state.is_patch:
        return ""

    clauses: list[str] = []

    revisions = patch_state.series
    if len(revisions) >= 2:
        current = next((e for e in revisions if e.is_current), None)
        if current is not None:
            # The count is the versions the archive holds, a resend of one
            # counting once (#861), not the series' length: "v7 of 5" read
            # as a contradiction (#844).
            clauses.append(
                f"revision {current.version} ({len(revisions)} revisions archived)"
            )
        else:
            clauses.append(f"one of {len(revisions)} revisions in this series")

    reviewed = [t for t in patch_state.trailers if t.role in REVIEW_TRAILER_ROLES]
    reviews = sum(t.total for t in reviewed)
    maintainer_reviews = sum(t.maintainer_count for t in reviewed)
    if reviews:
        clause = f"{reviews} review {'trailer' if reviews == 1 else 'trailers'}"
        if maintainer_reviews:
            clause += f" ({maintainer_reviews} from subsystem maintainers)"
        clauses.append(clause)

    if not clauses:
        return ""
    sentence = "; ".join(clauses)
    return sentence[0].upper() + sentence[1:] + "."


@bp_web.app_template_filter("status_line")
def _status_line_filter(status) -> str:
    """The lifecycle state as a sentence with its evidence (#844), or ""
    for PENDING (silence, as on the pill) and for no status. Reads only
    the pill's own `LifecycleStatusInfo`, so the state, the commit and
    the newer revision are the ones the pill decided on, in the pill's
    precedence (LANDED > SUPERSEDED > QUEUED > REVIEWED > PENDING)."""
    # Function-local: `mimir.patch_state` imports `mimir.web.urls`.
    from mimir.lifecycle_status import LifecycleStatus
    from mimir.patch_state import _resolve_tree_label

    if status is None:
        return ""
    state = status.state
    commit = f" as {status.commit_sha[:12]}" if status.commit_sha else ""
    if status.committed_at:
        commit += f" on {status.committed_at:%Y-%m-%d}"
    if state == LifecycleStatus.LANDED:
        return f"Landed in mainline{commit}."
    if state == LifecycleStatus.QUEUED:
        return f"Queued in {_resolve_tree_label(status.tree or '')}{commit}."
    if state == LifecycleStatus.SUPERSEDED:
        if not status.superseded_by_version:
            return "Superseded."
        posted = (
            f" (posted {status.superseded_at:%Y-%m-%d})" if status.superseded_at else ""
        )
        return f"Superseded by {status.superseded_by_version}{posted}."
    if state == LifecycleStatus.REVIEWED:
        return "Reviewed."
    return ""


@bp_web.app_template_filter("is_allowlisted_address")
def _is_allowlisted_address_filter(address: str | None) -> bool:
    """True iff `address` is in the allowlist (static tokens
    OR MAINTAINERS-derived set).

    Used by templates to decide whether to render a clickable
    reviewer link, alongside the `is_addressable_reviewer` syntax check.
    The reviewer page accepts supported address syntax regardless of
    the allowlist, but mimir only generates outbound links for
    allowlisted addresses, this keeps non-public addresses out of
    URL bars / browser history / scraper paths reached via mimir's
    own navigation, matching the redaction posture of `safe_from`.
    """
    if not address:
        return False
    return _is_allowlisted(address)


@bp_web.app_template_filter("subsystem_maintainers")
def _subsystem_maintainers_filter(hits) -> list[tuple[str, str]]:
    """`(name, address)` of each `M:` maintainer across `hits`, once
    each, in hit order. `R:` reviewers are left out: the profile route
    serves `M:` only (see `maintainer_link_url`).

    Deduped on the ADDRESS, not the name: the address is the profile's
    identity, and two sections can spell one person's name differently.
    MAINTAINERS permits a bare `M:` line with no address (rare but
    legal); that person is keyed on the name and keeps a plain-text
    entry rather than being dropped."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for hit in hits:
        for role, name, address in hit.maintainers:
            # A name that is itself a non-allowlisted address is no name
            # (#852), so it can never print an address as text.
            name = _usable_name(name)
            key = address.lower() if address else f"name:{name}"
            if role == "M" and name and key not in seen:
                seen.add(key)
                out.append((name, address))
    return out


@bp_web.app_template_filter("maintainer_link_url")
def _maintainer_link_url_filter(address: str | None) -> str:
    """Site-relative profile URL for a maintainer, or "" when the
    address must stay plain text. One filter so both link sites apply
    the same gate in the same order (#616).

    The URL comes from `maintainer_directory.maintainer_path`, so a link
    rendered in a template is byte-identical to the profile page's own
    canonical and to its sitemap entry.

    What makes a link safe is the caller's `role == 'M'` filter, NOT
    the allowlist gate. `/maintainers/<address>` serves `M:` only while
    the allowlist is the union of `M:` and `R:`, so every reviewer
    passes the gate and would get a 404 link. That was a shipped bug
    (MEMORY.md 2026-07-30).

    The gate is still applied, for the redaction question it does
    answer: an address the page would redact should not be linked. It
    is conditional rather than structural, since a deploy without the
    kernel tree has an empty MAINTAINERS-derived set and it fails closed
    for every non-`@kernel.org` maintainer. The syntax check keeps
    unsupported addresses as text rather than broken profile links.
    """
    address = address or ""
    if not (is_addressable_maintainer_address(address) and _is_allowlisted(address)):
        return ""
    return maintainer_path(address)


@bp_web.app_template_filter("subsystem_url")
def _subsystem_url_filter(name: str, inbox_name: str) -> str:
    """Site-relative URL for a subsystem dashboard. See
    `mimir.subsystems.subsystem_path` for why this is shared."""
    return subsystem_path(inbox_name, name)


@bp_web.app_template_filter("subsystem_index_url")
def _subsystem_index_url_filter(inbox_name: str) -> str:
    """Site-relative URL for an inbox's subsystem index. See
    `mimir.subsystems.subsystem_index_path`."""
    return subsystem_index_path(inbox_name)


@bp_web.app_template_filter("is_addressable_subsystem")
def _is_addressable_subsystem_filter(name: str) -> bool:
    """Whether a subsystem name has a URL the dashboard route accepts.
    See `mimir.subsystems.is_addressable_subsystem_name`."""
    return is_addressable_subsystem_name(name or "")
