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
from datetime import datetime, timezone
from email.utils import parseaddr

from flask import g
from pygments.lexers import get_lexer_for_filename, guess_lexer
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

from mimir import maintainer_allowlist
from mimir.config import settings
from mimir.datetime_utils import aware_utc
from mimir.models import Article
from mimir.rendering import render_body
from mimir.web._blueprint import bp_web
from mimir.web.urls import _msg_url, _thread_view_url


def _relative_time(then: datetime, now: datetime | None = None) -> str:
    """Render a coarse relative-time string for the closed-state fold
    summary ("23 messages, 5 authors, 2h ago"). Uses minutes/hours/days
    units under 30 days; falls back to an absolute YYYY-MM-DD beyond
    that, since "47d ago" is harder to parse than the date itself."""
    if now is None:
        now = datetime.now(timezone.utc)
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


def _thread_summary(thread) -> dict:
    """Compute the headline stats shown in the `closed` fold state:
    total message count, unique-author count (by email so display-name
    drift doesn't fragment the tally), and a coarse relative-time
    string for the most-recent message in the thread."""
    if not thread:
        return {"author_count": 0, "last_activity_rel": "?"}
    emails: set[str] = set()
    for n in thread:
        if not n.author:
            continue
        _, addr = parseaddr(n.author)
        if addr:
            emails.add(addr.lower())
    dates = [n.date for n in thread if n.date]
    last = max(dates) if dates else None
    return {
        "author_count": len(emails) or len(thread),
        "last_activity_rel": _relative_time(last) if last else "?",
    }


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
    if any(token.lower() in addr_lower for token in settings.email_allowlist):
        return True
    try:
        cached = g.setdefault(
            "_maintainer_addresses",
            maintainer_allowlist.maintainer_addresses(),
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


@bp_web.app_template_filter("thread_url")
def _thread_url_filter(article: Article, inbox_name: str) -> str:
    """URL of the whole-thread view rooted at `article`.

    Listings link the subject to the message page and the reply count
    to here, so the thread view gains inbound links from the pages that
    actually get crawled. It is the canonical target for every message
    in a multi-message thread and the URL the sitemap lists, yet before
    this the entire template set contained exactly ONE link to it
    (`message.html`, conditional), while ten templates linked message
    pages. A canonical target reachable only through the pages that
    disclaim themselves is a weak structure, whatever the canonical
    tag says.

    Callers must gate on the thread actually having replies; `/t` on a
    single-message thread is a poorer page than the message itself and
    deliberately not its canonical.
    """
    return _thread_view_url(article, inbox_name)


@bp_web.app_template_filter("render_body")
def _render_body_filter(
    body,
    msgid_urls=None,
    parent_url=None,
    lore_mirror_urls=None,
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


# Trailer roles that count as review feedback for the synthesis line.
# Mirrors `_REVIEW_TRAILER_ROLES` in `mimir/subsystems_dashboard/triage.py`
# (which `mimir/lifecycle_status.py` also mirrors): Signed-off-by is
# authorship and Reported-by is bug attribution, neither is review.
# Duplicated rather than shared, following the existing convention for
# this tuple; three strings don't earn a shared module.
_REVIEW_TRAILER_ROLES = ("Reviewed-by", "Acked-by", "Tested-by")

# Linus's tree is the one that means "mainline". Mirrors the
# `tree_name = 'linus'` literal in `mimir/lifecycle_status.py`'s bulk
# SQL, which is what decides LANDED vs QUEUED on the badge this
# sentence restates.
_LINUS_TREE_NAME = "linus"


@bp_web.app_template_filter("patch_synthesis")
def _patch_synthesis_filter(patch_state) -> str:
    """One-sentence, human-readable summary of a patch's lifecycle,
    composed from data the state card already carries.

    Exists for SEO index-shaping (design doc 2026-07-27, W3b): the
    badges and the state card encode this same information as glyphs
    and pills, which read well for a human scanning but give a crawler
    nothing indexable. Rendering it once as prose gives the page
    unique, query-matching text ("landed in net-next as abc123") that
    no other LKML mirror emits, without changing what the badges do.

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
            clauses.append(
                f"revision {current.version} of {len(revisions)} in this series"
            )
        else:
            clauses.append(f"one of {len(revisions)} revisions in this series")

    reviewed = [t for t in patch_state.trailers if t.role in _REVIEW_TRAILER_ROLES]
    reviews = sum(t.total for t in reviewed)
    maintainer_reviews = sum(t.maintainer_count for t in reviewed)
    if reviews:
        clause = f"{reviews} review {'trailer' if reviews == 1 else 'trailers'}"
        if maintainer_reviews:
            clause += f" ({maintainer_reviews} from subsystem maintainers)"
        clauses.append(clause)

    # Mirror the lifecycle badge's tree priority. A patch that reached
    # mainline routinely carries several landings (subsystem tree, then
    # linux-next, then Linus), ordered oldest-first, so taking the first
    # row would report "landed in net-next" directly under a LANDED
    # badge showing the Linus sha. Answering "did this land in
    # mainline?" is the whole point of the sentence, so Linus wins when
    # present; otherwise report the earliest other tree, which is what
    # the QUEUED badge shows.
    landings = patch_state.mainline_landings
    landed = next((c for c in landings if c.tree_name == _LINUS_TREE_NAME), None)
    queued = next((c for c in landings if c.tree_name != _LINUS_TREE_NAME), None)
    if landed is not None:
        clause = f"landed in mainline as {landed.commit_sha[:12]}"
        if landed.committed_at is not None:
            clause += f" on {landed.committed_at:%Y-%m-%d}"
        clauses.append(clause)
    elif queued is not None:
        clause = f"queued in {queued.tree_label} as {queued.commit_sha[:12]}"
        if queued.committed_at is not None:
            clause += f" on {queued.committed_at:%Y-%m-%d}"
        clauses.append(clause)

    if not clauses:
        return ""
    sentence = "; ".join(clauses)
    return sentence[0].upper() + sentence[1:] + "."


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


@bp_web.app_template_filter("maintainer_url")
def _maintainer_url_filter(address: str | None) -> str:
    """Site-relative URL for a maintainer's profile page.

    Delegates to `maintainer_directory.maintainer_path` so a link
    rendered in a template is byte-identical to the profile page's own
    canonical and to its sitemap entry, rather than a near-miss that
    resolves to them.

    Callers gate on `is_allowlisted_address` first, same posture as the
    reviewer links above. MAINTAINERS `M:`/`R:` addresses are what the
    allowlist is built FROM, so the gate passes by construction; it is
    there so the invariant is enforced rather than assumed, and so a
    future narrowing of the allowlist cannot silently start emitting
    links to addresses the page redacts.
    """
    from mimir.maintainer_directory import maintainer_path

    return maintainer_path(address or "")


@bp_web.app_template_filter("subsystem_url")
def _subsystem_url_filter(name: str, inbox_name: str) -> str:
    """Site-relative URL for a subsystem dashboard. See
    `mimir.subsystems.subsystem_path` for why this is shared."""
    from mimir.subsystems import subsystem_path

    return subsystem_path(inbox_name, name)
