import json
import logging
import re
import secrets
import time
from datetime import date as date_cls, datetime, timedelta, timezone
from email.utils import parseaddr
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, tostring

from pathlib import Path

from flask import (
    Blueprint, Response, abort, g, redirect, render_template, request,
    send_from_directory,
)
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename, guess_lexer
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from mimir import cache, maintainer_allowlist
from mimir.canonical import extract_list_addresses
from mimir.config import settings
from mimir.dashboard import (
    ArticleSummary,
    archive_stats,
    author_recent,
    daily_volume,
    latest_pull_requests,
    latest_stable_releases,
    monthly_volume,
    recent_articles,
    search_articles,
    this_day_in_history,
)
from mimir.extensions import SessionLocal
from mimir.inboxes import inbox_names
from mimir.models import (
    Article, ArticleFile, ArticleList, Inbox, MainlineCommit, Subsystem,
)
from mimir.parser import ParsedArticle
from mimir.rendering import URL_OR_MSGID_RE, redact_trailer_addresses, render_body
from mimir.store import MessageNotFound, read_message
from mimir.subsystems import (
    REVIEWS_PER_PAGE_LIMIT,
    active_reviewers_in_subsystem,
    active_threads_in_subsystem,
    articles_reviewed_by,
    daily_volume_in_subsystem,
    most_active_subsystems_global,
    most_active_subsystems_in_inbox,
    recent_articles_in_subsystem,
    recent_patches_touching,
    subsystems_for_article,
)
from mimir.threading import (
    THREADS_SINCE_MAX_DAYS,
    active_threads,
    find_thread_root,
    get_thread,
    threads_for_day,
    threads_for_month,
    threads_since,
)

bp_web = Blueprint("web", __name__)


def _get_inbox_or_404(session: Session, name: str) -> Inbox:
    """Resolve URL slug → Inbox row. Single source of truth for whether
    a `/<inbox_name>/...` URL is valid."""
    inbox = session.execute(
        select(Inbox).where(Inbox.name == name)
    ).scalar_one_or_none()
    if inbox is None:
        abort(404)
    return inbox


@bp_web.app_context_processor
def _inject_template_globals() -> dict:
    """Inboxes are needed by base.html for the nav. `current_inbox` is set
    per-view (None on the meta-index `/`). Names come from the cached
    list populated at bootstrap — no per-request DB hit. `site_name` is
    the configurable brand; "mimir" stays as the page generator.

    `default_canonical_url` is the https-safe fallback for og:url on
    routes that don't pin their own canonical (search, daily, year,
    month, author …). Built from `_site_base()` so SITE_BASE_URL
    forces correctness even when ProxyFix isn't in scope.
    """
    from flask import has_request_context

    from mimir import __version__ as mimir_version
    site_base = ""
    default_canonical = ""
    if has_request_context():
        site_base = _site_base()
        default_canonical = site_base + request.path
    return {
        "inboxes": inbox_names(),
        "site_name": settings.site_name,
        "mimir_version": mimir_version,
        "site_base": site_base,
        "default_canonical_url": default_canonical,
        "og_image_alt": OG_IMAGE_ALT,
    }


# Cache-Control per endpoint. Lets edge caches (Cloudflare, an nginx in
# front, the browser) absorb scraper traffic on the high-volume endpoints
# without pinning stale content for too long. Attachments are
# byte-stable per (message_id, n), so they're cached aggressively;
# listings are cached briefly so new messages don't take more than ~1
# minute to surface; pagination and 404s/redirects skip caching.
_CACHE_CONTROL_BY_ENDPOINT = {
    "web.index": "public, max-age=300",
    "web.inbox_dashboard": "public, max-age=60",
    "web.daily_today": "public, max-age=60",
    "web.daily_yesterday": "public, max-age=600",
    "web.threads_since_view": "public, max-age=600",
    "web.subsystem_dashboard": "public, max-age=600",
    "web.year_archive": "public, max-age=600",
    "web.month_archive": "public, max-age=600",
    "web.search": "public, max-age=300",
    "web.author_view": "public, max-age=300",
    "web.reviewer_view": "public, max-age=600",
    "web.inbox_feed": "public, max-age=300",
    "web.author_feed": "public, max-age=300",
    "web.robots": "public, max-age=86400",
    "web.security_txt": "public, max-age=3600",
    "web.favicon_svg": "public, max-age=604800",
    "web.og_image_png": "public, max-age=604800",
    "web.sitemap": "public, max-age=300",
    "web.meta_sitemap": "public, max-age=300",
    "web.inbox_sitemap": "public, max-age=300",
    "web.message_id_lookup": "public, max-age=3600",
    "web.message_id_lookup_inbox": "public, max-age=3600",
    "web.message": "public, max-age=60",
    "web.attachment_download": "public, max-age=3600, immutable",
    "web.attachment_preview": "public, max-age=3600, immutable",
}


# Defense-in-depth response headers. Applied to every response so 404s
# and error pages also get them.
#
# CSP: HTML escaping is correct, but a CSP narrows the blast radius of
#   any future bug. `default-src 'self'` plus the two CDNs we SRI-pin;
#   inline styles allowed because Pico CSS uses them and Pygments
#   emits inline `style=` for highlighted code.
# Referrer-Policy: don't leak full URLs (which include Message-IDs and
#   inbox names) to outbound links.
# X-Content-Type-Options: forces browsers to honor the Content-Type we
#   send rather than sniffing.
# X-Frame-Options: trivial anti-clickjacking. mimir has no embed use
#   case.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "script-src 'self' https://unpkg.com; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    ),
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


# One JSON object per request to stdout. Doesn't propagate to root,
# so it doesn't double-log via app.logger if anyone reconfigures it.
_request_logger = logging.getLogger("mimir.request")
_request_logger.propagate = False
if not _request_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    _request_logger.addHandler(_h)
    _request_logger.setLevel(logging.INFO)


@bp_web.before_request
def _start_request_timer():
    g._request_t0 = time.perf_counter()
    # Honor an upstream-supplied request id (typical reverse-proxy
    # pattern) so multi-hop traces stay correlatable; otherwise mint
    # a fresh short id.
    g._request_id = (
        request.headers.get("X-Request-Id") or secrets.token_hex(8)
    )


@bp_web.after_request
def _add_cache_headers(response):
    # 200 OK and 301/302 redirects (Message-ID lookup) are cacheable;
    # honor their dict entries. 4xx/5xx skip — error responses
    # shouldn't be pinned in upstream caches.
    if response.status_code in (200, 301, 302):
        rule = _CACHE_CONTROL_BY_ENDPOINT.get(request.endpoint)
        if rule:
            response.headers["Cache-Control"] = rule
    # The message route returns a partial (`_message_body.html`) under
    # `HX-Request: true` and the full page otherwise. Without Vary,
    # caches (browser bfcache, Cloudflare, Chrome's prerender cache
    # for sites with speculation rules) can serve a full-page response
    # to an HTMX request, which then swaps the entire <body>'s
    # children into `#msg` and visibly duplicates the page chrome.
    # The Vary header keys the two response variants separately.
    if request.endpoint == "web.message":
        response.headers["Vary"] = "HX-Request"
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    # HSTS only when we know the request came in over HTTPS — set
    # behind a reverse proxy that forwards X-Forwarded-Proto. Otherwise
    # an http://localhost dev session would tell the browser "force
    # https on this host forever," which would break the dev workflow.
    if request.headers.get("X-Forwarded-Proto") == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    response.headers.setdefault("X-Request-Id", getattr(g, "_request_id", "-"))
    return response


@bp_web.after_request
def _log_request(response):
    """Emit one JSON-line access record per request. Runs after the
    cache + security headers are set so duration covers the full
    response-build path."""
    t0 = getattr(g, "_request_t0", None)
    duration_ms = round((time.perf_counter() - t0) * 1000, 1) if t0 else None
    _request_logger.info(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "request_id": getattr(g, "_request_id", None),
        "method": request.method,
        "path": request.path,
        "status": response.status_code,
        "duration_ms": duration_ms,
        "remote": request.remote_addr,
        # Read the header directly: Werkzeug's request.user_agent is a
        # UserAgent wrapper whose __bool__ depends on the bundled UA
        # parser detecting a known browser, which makes legitimate
        # values like "curl/8.20.0" — and, with this Werkzeug, plain
        # Firefox — evaluate falsy and silently turn into null. The
        # raw header is what we actually want to log.
        "ua": request.headers.get("User-Agent"),
        "referrer": request.referrer,
    }))
    return response


def _site_base() -> str:
    """Return the absolute base URL for emitted links, no trailing slash.

    Prefers the explicit `SITE_BASE_URL` setting when set — that's the
    deterministic override for production where ProxyFix may or may
    not be wired correctly across the Tailscale Funnel + Caddy chain.
    Falls back to `request.url_root` for local-dev and any deployment
    that doesn't supply the override; if `X-Forwarded-Proto: https` is
    present but ProxyFix didn't translate it (wrong hop count, header
    not in the trusted set), we still upgrade the scheme. Otherwise
    canonical / og:url / og:image / JSON-LD URLs split between http and
    https on the same page when only one of those signals is wired.
    """
    if settings.site_base_url:
        return settings.site_base_url.rstrip("/")
    base = request.url_root.rstrip("/")
    if (
        request.headers.get("X-Forwarded-Proto") == "https"
        and base.startswith("http://")
    ):
        base = "https://" + base[len("http://"):]
    return base


def _msg_url(article: Article, inbox_name: str) -> str:
    """Build the canonical /<list>/YYYY/MM/<id> URL for an Article in
    `inbox_name`. With cross-posts, the same article can render at
    multiple URLs (one per inbox it's linked to); the caller picks
    based on context (the URL's inbox)."""
    if article.date is not None:
        return f"/{inbox_name}/{article.date.year}/{article.date.month:02d}/{article.id}"
    return f"/{inbox_name}/0000/00/{article.id}"


def _canonical_inbox_name(
    article: Article,
    links: list[tuple[int, str]],
) -> str | None:
    """Pick the canonical inbox name for `article` from the list of
    `(inbox_id, inbox_name)` tuples it's linked to. Uses
    `article.canonical_inbox_id` when set; falls back to the
    alphabetically-first link — stable across renders so the SEO
    signal doesn't flicker between equivalent cross-posts. Returns
    None only when `links` is empty (a corrupt row; should never
    happen given FK cascades)."""
    canonical_id = article.canonical_inbox_id
    if canonical_id is not None:
        for ix_id, name in links:
            if ix_id == canonical_id:
                return name
    if not links:
        return None
    return min(name for _, name in links)


def _year_decade_groups(first_year: int, last_year: int) -> list[tuple[int, list[int]]]:
    """Group `[first_year, last_year]` into decade buckets, newest first.

    Returns a list like `[(2020, [2026, 2025, 2024, ...]), (2010, [2019,
    ..., 2010]), ...]`. Each inner list is descending. Drives the year-
    browse footer on the inbox dashboard; reads better than a flat 30-
    item row on narrow viewports.
    """
    if last_year < first_year:
        return []
    groups: dict[int, list[int]] = {}
    for year in range(last_year, first_year - 1, -1):
        decade = (year // 10) * 10
        groups.setdefault(decade, []).append(year)
    return sorted(groups.items(), key=lambda kv: kv[0], reverse=True)


def _canonical_url_for(
    article: Article,
    links: list[tuple[int, str]],
    base: str = "",
) -> str | None:
    """Compose the full canonical URL for `article` using `links`
    (list of `(inbox_id, inbox_name)`). Returns None when no inbox
    can be resolved."""
    inbox_name = _canonical_inbox_name(article, links)
    if inbox_name is None:
        return None
    return base + _msg_url(article, inbox_name)


def _relative_time(then: datetime, now: datetime | None = None) -> str:
    """Render a coarse relative-time string for the closed-state fold
    summary ("23 messages, 5 authors, 2h ago"). Uses minutes/hours/days
    units under 30 days; falls back to an absolute YYYY-MM-DD beyond
    that, since "47d ago" is harder to parse than the date itself."""
    if now is None:
        now = datetime.now(timezone.utc)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
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


@bp_web.app_template_filter("msg_url")
def _msg_url_filter(article: Article, inbox_name: str) -> str:
    return _msg_url(article, inbox_name)


@bp_web.app_template_filter("render_body")
def _render_body_filter(body, msgid_urls=None, parent_url=None):
    return render_body(
        body,
        msgid_urls=msgid_urls,
        address_redactor=_redact_trailer_address,
        parent_url=parent_url,
    )


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


@bp_web.app_template_filter("is_previewable")
def _is_previewable_filter(att) -> bool:
    return _is_previewable(att)


def _is_allowlisted(address: str) -> bool:
    """Union check: static `Settings.email_allowlist` substring tokens
    OR exact membership in the dynamic MAINTAINERS-derived address
    set. Both checks operate on the lowercased address.

    Per-request memoised via `flask.g` so the MAINTAINERS set is
    fetched at most once per render (the underlying DB-backed cache
    is fast, but a long page can call this 50+ times — `g`-caching
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
    snippets. Allowlisted senders also surface just their display name —
    consistency over leaking addresses into descriptions. Falls back to
    'unknown sender' so the snippet doesn't render with an awkward
    trailing punctuation hole."""
    if not author:
        return "unknown sender"
    name, _ = parseaddr(author)
    if name:
        return name
    return "unknown sender"


@bp_web.app_template_filter("is_allowlisted_address")
def _is_allowlisted_address_filter(address: str | None) -> bool:
    """True iff `address` is in the allowlist (static tokens
    OR MAINTAINERS-derived set).

    Used by templates to decide whether to render a clickable
    reviewer link. The reviewer page itself (`/<inbox>/reviewer/<addr>`)
    accepts any address, but mimir only generates outbound links for
    allowlisted addresses — this keeps non-public addresses out of
    URL bars / browser history / scraper paths reached via mimir's
    own navigation, matching the redaction posture of `safe_from`.
    """
    if not address:
        return False
    return _is_allowlisted(address)


def _redact_trailer_address(email: str) -> str:
    """Return the visible-text replacement for an email on a DCO
    trailer line. Allowlisted addresses survive verbatim so the DCO
    chain stays verifiable for known maintainers; everyone else gets
    `<redacted>` — a placeholder that obviously isn't broken metadata
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


RECENT_PAGE_SIZE = 10


def _fetch_recent(session: Session, inbox: Inbox, offset: int, limit: int):
    """Fetch limit+1 recent articles in `inbox` to detect has_more cheaply.

    Filtered via EXISTS rather than JOIN: with parameterized
    `inbox_id=?`, SQLite's planner mis-prices the JOIN form and picks
    a full scan-and-sort over `article_lists`, taking seconds. The
    EXISTS form makes "walk articles by date desc, probe article_lists
    via composite PK" the obvious plan, matching what literal binds
    would have done.
    """
    in_inbox = (
        select(ArticleList.article_id)
        .where(
            ArticleList.article_id == Article.id,
            ArticleList.inbox_id == inbox.id,
        )
        .exists()
    )
    rows = session.execute(
        select(Article)
        .where(in_inbox)
        .order_by(Article.date.desc().nulls_last())
        .offset(offset)
        .limit(limit + 1)
    ).scalars().all()
    has_more = len(rows) > limit
    return rows[:limit], has_more


@bp_web.route("/healthz")
def healthz():
    """Cheap liveness probe — confirms the app factory ran. No DB
    work; load balancers / orchestrators can hit this on the seconds-
    cadence they want."""
    return Response("ok\n", mimetype="text/plain", headers={"Cache-Control": "no-store"})


@bp_web.route("/readyz")
def readyz():
    """Readiness probe — also confirms the DB is reachable via a
    `SELECT 1`. Slightly more expensive than /healthz; use for the
    'serving traffic' decision, not for liveness restarts."""
    try:
        with SessionLocal() as session:
            session.execute(select(1))
    except Exception as exc:  # pragma: no cover - defensive
        return Response(
            f"db unreachable: {exc!r}\n",
            status=503,
            mimetype="text/plain",
            headers={"Cache-Control": "no-store"},
        )
    return Response("ok\n", mimetype="text/plain", headers={"Cache-Control": "no-store"})


@bp_web.route("/robots.txt")
def robots():
    """Static robots.txt — disallows attachment downloads (saves bot
    bandwidth on binaries) and points crawlers at the sitemap."""
    sitemap_url = _site_base() + "/sitemap.xml"
    body = render_template("robots.txt", sitemap_url=sitemap_url)
    return Response(body, mimetype="text/plain; charset=utf-8")


# Placeholder squirrel-adjacent emoji until a proper logo lands
# (tracked in CONTEXT.md "Roadmap — Favicon / logo"). Inline SVG keeps
# the file at a couple of hundred bytes and avoids a static-folder
# dependency. Browsers cache the response aggressively per the
# `_CACHE_CONTROL_BY_ENDPOINT` map.
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<text x="0" y="14" font-size="14">🐿️</text></svg>'
)


@bp_web.route("/favicon.svg")
def favicon_svg():
    return Response(_FAVICON_SVG, mimetype="image/svg+xml")


# OG image: 1200x630 PNG composited from a 17th-century Icelandic
# Edda manuscript depiction of Ratatoskr (AM 738 4to era, Árni
# Magnússon Institute, public domain). Twitter/X doesn't render SVG
# and LinkedIn is inconsistent on it; PNG is the safer baseline. The
# asset is pre-baked by `bake_og_image.py` and checked in under
# `static/img/`; this route exists to keep the URL at the site root
# (matches the prior `/og-image.svg` shape and reads as deliberate
# branding rather than a deep static-folder path).
OG_IMAGE_FILENAME = "og-image.png"
OG_IMAGE_WIDTH = 1200
OG_IMAGE_HEIGHT = 630
OG_IMAGE_ALT = (
    "Ratatoskr from a 17th-century Icelandic Edda manuscript "
    "(AM 738 4to era, Árni Magnússon Institute, public domain), "
    "next to the ratatoskr.run wordmark."
)


_STATIC_IMG_DIR = Path(__file__).resolve().parent / "static" / "img"


@bp_web.route("/og-image.png")
def og_image_png():
    return send_from_directory(_STATIC_IMG_DIR, OG_IMAGE_FILENAME, mimetype="image/png")


@bp_web.route("/security.txt")
@bp_web.route("/.well-known/security.txt")
def security_txt():
    """RFC 9116 security.txt. 404 unless `SECURITY_CONTACT` is set —
    don't ship a contact-less file. The Expires field is computed at
    request time as `now + 1 year` so it never falls into the past."""
    if not settings.security_contact:
        abort(404)
    expires = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat(timespec="seconds")
    body = render_template(
        "security.txt",
        contact=settings.security_contact,
        expires=expires,
        preferred_languages=settings.security_preferred_languages,
        policy_url=settings.security_policy_url,
        encryption_url=settings.security_encryption_url,
    )
    return Response(body, mimetype="text/plain; charset=utf-8")


# Site-wide tagline. Mirrored verbatim by base.html's default
# meta_description block so the WebSite JSON-LD and the meta tag
# can't drift. If you change one, change the other.
DEFAULT_SITE_DESCRIPTION = (
    "Indexed mailing-list archives, served from local "
    "public-inbox v2 mirrors."
)


def _json_ld_index(base: str, inboxes=()) -> dict:
    """schema.org WebSite for the meta-index `/`, paired with an
    `ItemList` of configured inboxes so search engines can treat the
    page as a topical hub rather than a flat link list. Sitelinks-
    search-box is intentionally omitted (mimir's search is per-inbox,
    not site-wide)."""
    payload: dict = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": settings.site_name,
        "url": base + "/",
        "description": DEFAULT_SITE_DESCRIPTION,
    }
    if inboxes:
        payload["mainEntity"] = {
            "@type": "ItemList",
            "name": f"Inboxes indexed by {settings.site_name}",
            "numberOfItems": len(inboxes),
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": i + 1,
                    "url": f"{base}/{inbox.name}/",
                    "name": inbox.name,
                }
                for i, inbox in enumerate(inboxes)
            ],
        }
    return payload


def _json_ld_inbox(base: str, inbox, active_threads=()) -> dict:
    """schema.org payload for `/<inbox_name>/` — a `DiscussionForum`
    container plus an `ItemList` of the currently-most-active threads
    so the page reads as a topical hub for crawlers. `active_threads`
    is whatever the dashboard fetched (root-level ThreadNode objects);
    we project just the bits search engines care about (URL + name).
    """
    payload: dict = {
        "@context": "https://schema.org",
        "@type": "DiscussionForum",
        "name": inbox.name,
        "url": f"{base}/{inbox.name}/",
    }
    if active_threads:
        payload["mainEntity"] = {
            "@type": "ItemList",
            "name": f"Most active threads in {inbox.name}",
            "numberOfItems": len(active_threads),
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": i + 1,
                    "url": f"{base}{_msg_url(t, inbox.name)}",
                    "name": _clean_subject_filter(t.subject) or "(no subject)",
                }
                for i, t in enumerate(active_threads)
            ],
        }
    return payload


JSON_LD_TEXT_MAX = 2000


def _json_ld_text_snippet(body: str | None) -> str | None:
    """Return a plaintext snippet of `body` suitable for the
    DiscussionForumPosting `text` field, or None when there's nothing
    usable. Whitespace is collapsed (mail bodies have lots of hard
    wraps that read as paragraph noise in JSON-LD); truncation
    happens at the last whitespace inside JSON_LD_TEXT_MAX so we
    don't slice mid-word, with a trailing ellipsis when we did
    truncate. Returning None lets the caller omit the field
    entirely — emitting an empty string would re-fail Google's
    "either text/image/video" validator."""
    if not body:
        return None
    collapsed = " ".join(body.split())
    if not collapsed:
        return None
    if len(collapsed) <= JSON_LD_TEXT_MAX:
        return collapsed
    head = collapsed[:JSON_LD_TEXT_MAX]
    cut = head.rfind(" ")
    # Pathological no-space body: hard-cut at the limit rather than
    # returning the entire string just because rfind didn't find a
    # break point.
    if cut <= 0:
        cut = JSON_LD_TEXT_MAX
    return head[:cut].rstrip() + "..."


def _json_ld_message(
    article: Article,
    parsed: ParsedArticle,
    canonical_url: str,
    inbox_name: str,
    base: str,
) -> dict:
    """schema.org @graph carrying both DiscussionForumPosting (the
    primary signal — eligible for Google's "Discussions and forums"
    rich-result section) and BreadcrumbList (surfaces the
    Site → Inbox → Subject chain in SERPs).

    Author goes through `_display_name_filter` — display name only,
    no email and no `<hidden>` placeholder. The placeholder is a
    rendering decision for the visible HTML; in machine-readable
    metadata it reads as broken data and was flagged as such in the
    2026-05-12 review. `author.url` points at the per-inbox author
    view so the Person has a stable target for "more posts by this
    author"; required-by-Google for the Discussions rich-result
    eligibility (non-critical, Search Console 2026-05-14).
    `dateModified` mirrors `datePublished` because mimir doesn't
    track edits.

    `text` carries a plain-text snippet of `parsed.body`, capped at
    JSON_LD_TEXT_MAX chars (truncated at the last whitespace inside
    the cap) — Google's DiscussionForumPosting validator treats one
    of `text` / `image` / `video` as required (critical, Search
    Console 2026-05-14). Omitted entirely when the body is missing
    or whitespace-only: an empty string would re-fail the validator.

    Prefers `parsed.date` (the original RFC 5322 Date header) over
    `article.date` (the public-inbox commit time) — the message's
    actual send date is more meaningful to search engines."""
    raw_date = parsed.date or article.date
    if raw_date is not None and raw_date.tzinfo is None:
        # `-0000` Date headers come back tz-naive from
        # parsedate_to_datetime; emit aware UTC so consumers don't
        # see schema-invalid bare datetimes.
        raw_date = raw_date.replace(tzinfo=timezone.utc)
    iso_date = (
        raw_date.strftime("%Y-%m-%dT%H:%M:%S%z") if raw_date else None
    )
    subject = parsed.subject or "(no subject)"
    breadcrumb_subject = subject if len(subject) <= 80 else subject[:77] + "..."
    author_name = _display_name_filter(parsed.author)
    author: dict = {"@type": "Person", "name": author_name}
    # Per-inbox author view is a substring match on the From field;
    # the display name is exactly what'll match the author's other
    # posts. Skip the URL when we fell back to "unknown sender" —
    # that token doesn't match anyone.
    if author_name and author_name != "unknown sender":
        author["url"] = f"{base}/{inbox_name}/author/{quote(author_name, safe='')}"
    forum_post: dict = {
        "@type": "DiscussionForumPosting",
        "@id": canonical_url,
        "url": canonical_url,
        "mainEntityOfPage": canonical_url,
        "headline": subject,
        "author": author,
        "isPartOf": {
            "@type": "WebSite",
            "name": inbox_name,
            "url": f"{base}/{inbox_name}/",
        },
    }
    # Apply the same DCO trailer redaction the visible HTML uses
    # before snippeting — JSON-LD `text` is yet another surface a
    # crawler scrapes, and CONTEXT.md's redaction invariants treat
    # every surface uniformly. Without this, non-allowlisted
    # Signed-off-by addresses would leak through the structured
    # data even though the rendered page redacts them.
    redacted_body = (
        redact_trailer_addresses(parsed.body, _redact_trailer_address)
        if parsed.body else parsed.body
    )
    body_snippet = _json_ld_text_snippet(redacted_body)
    if body_snippet:
        forum_post["text"] = body_snippet
    if iso_date:
        forum_post["datePublished"] = iso_date
        forum_post["dateModified"] = iso_date
    return {
        "@context": "https://schema.org",
        "@graph": [
            forum_post,
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": 1,
                        "name": settings.site_name,
                        "item": base + "/",
                    },
                    {
                        "@type": "ListItem",
                        "position": 2,
                        "name": inbox_name,
                        "item": f"{base}/{inbox_name}/",
                    },
                    {
                        "@type": "ListItem",
                        "position": 3,
                        "name": breadcrumb_subject,
                        "item": canonical_url,
                    },
                ],
            },
        ],
    }


def _json_ld_search(
    base: str, inbox_name: str, query: str, canonical_url: str,
) -> dict:
    """schema.org `SearchResultsPage` for `/<inbox_name>/search?q=…`.
    Emitted only when the route is rendering actual results (the
    no-query / too-short forms are just a search box, not a results
    page). `url` mirrors the canonical, which strips the query
    string — same SEO posture as the `<link rel="canonical">`: this
    is the page-shape, not the result-set."""
    return {
        "@context": "https://schema.org",
        "@type": "SearchResultsPage",
        "name": f"Search results for '{query}' in {inbox_name}",
        "url": canonical_url,
        "description": f"Search results for '{query}' in {inbox_name}.",
        "isPartOf": {
            "@type": "WebSite",
            "name": inbox_name,
            "url": f"{base}/{inbox_name}/",
        },
    }


def _json_ld_author(
    base: str, inbox_name: str, sub: str, canonical_url: str,
) -> dict:
    """schema.org `ProfilePage` for `/<inbox_name>/author/<sub>`. The
    `mainEntity` is a `Person` whose `name` is the sender substring
    we matched against — usually a full email or a domain like
    `@kernel.org`, sometimes a personal display-name fragment. We
    don't try to resolve it to a single identity (the substring may
    match many people, deliberately so for `@kernel.org`-shaped
    queries); `name` is the literal token the page is indexed against.
    """
    return {
        "@context": "https://schema.org",
        "@type": "ProfilePage",
        "name": f"Messages from {sub} in {inbox_name}",
        "url": canonical_url,
        "description": (
            f"Recent messages from senders matching '{sub}' "
            f"in the {inbox_name} archive."
        ),
        "mainEntity": {
            "@type": "Person",
            "name": sub,
        },
        "isPartOf": {
            "@type": "WebSite",
            "name": inbox_name,
            "url": f"{base}/{inbox_name}/",
        },
    }


SITEMAP_RECENT_PER_INBOX = 5000
SITEMAP_TTL_SEC = 3600


def _build_sitemap_xml(entries: list[tuple[str, str | None]]) -> str:
    """Render an XML <urlset> sitemap. Each entry is
    `(loc, lastmod | None)`; when `lastmod` is None the element is
    omitted. Caller formats the timestamp — date-only `YYYY-MM-DD`
    is what Google's docs recommend for crawl-scheduling and is what
    mimir emits."""
    root = Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for loc, lastmod in entries:
        url_el = SubElement(root, "url")
        SubElement(url_el, "loc").text = loc
        if lastmod:
            SubElement(url_el, "lastmod").text = lastmod
    return '<?xml version="1.0" encoding="utf-8"?>\n' + tostring(root, encoding="unicode")


def _build_sitemap_index_xml(entries: list[tuple[str, str | None]]) -> str:
    """Render a <sitemapindex> referencing sub-sitemaps. Same
    `(loc, lastmod)` shape as `_build_sitemap_xml`; the schema and
    element names differ — `<sitemapindex>` of `<sitemap>` rather
    than `<urlset>` of `<url>`."""
    root = Element(
        "sitemapindex", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
    )
    for loc, lastmod in entries:
        sm = SubElement(root, "sitemap")
        SubElement(sm, "loc").text = loc
        if lastmod:
            SubElement(sm, "lastmod").text = lastmod
    return '<?xml version="1.0" encoding="utf-8"?>\n' + tostring(root, encoding="unicode")


def _per_inbox_latest_date(session) -> dict[str, str | None]:
    """One round-trip: max(article.date) per inbox, as `YYYY-MM-DD`
    strings (or None when the inbox is empty). Feeds the sitemap
    index `<lastmod>` per sub-sitemap."""
    rows = session.execute(
        select(Inbox.name, func.max(Article.date))
        .join(ArticleList, ArticleList.inbox_id == Inbox.id)
        .join(Article, Article.id == ArticleList.article_id)
        .where(Article.date.is_not(None))
        .group_by(Inbox.id)
    ).all()
    return {name: (dt.strftime("%Y-%m-%d") if dt else None) for name, dt in rows}


def sitemap_index_xml(session: Session, base: str, *, force: bool = False) -> str:
    """Cached body of `/sitemap.xml`. Lists `/meta-sitemap.xml` plus one
    `/<inbox>/sitemap.xml` per configured inbox. Same cache key as the
    route uses (`sitemap:index`) so `warm-cache` can pre-populate it
    via `force=True`."""
    def compute() -> str:
        per_inbox_latest = _per_inbox_latest_date(session)
        global_latest = max(
            (d for d in per_inbox_latest.values() if d), default=None
        )
        entries: list[tuple[str, str | None]] = [
            (f"{base}/meta-sitemap.xml", global_latest),
        ]
        inboxes = session.execute(
            select(Inbox).order_by(Inbox.name)
        ).scalars().all()
        for inbox in inboxes:
            entries.append((
                f"{base}/{inbox.name}/sitemap.xml",
                per_inbox_latest.get(inbox.name),
            ))
        return _build_sitemap_index_xml(entries)

    return cache.get_or_compute(
        session, "sitemap:index", SITEMAP_TTL_SEC, compute, force=force,
    )


def meta_sitemap_xml(session: Session, base: str, *, force: bool = False) -> str:
    """Cached body of `/meta-sitemap.xml`. One-URL urlset covering `/`
    with the global-max article date as lastmod."""
    def compute() -> str:
        per_inbox_latest = _per_inbox_latest_date(session)
        global_latest = max(
            (d for d in per_inbox_latest.values() if d), default=None
        )
        return _build_sitemap_xml([(base + "/", global_latest)])

    return cache.get_or_compute(
        session, "sitemap:meta", SITEMAP_TTL_SEC, compute, force=force,
    )


def inbox_sitemap_xml(
    session: Session, inbox: Inbox, base: str, *, force: bool = False
) -> str:
    """Cached body of `/<inbox>/sitemap.xml`. Dashboard + year/month
    archives that actually have data + the SITEMAP_RECENT_PER_INBOX
    most-recent article URLs in this inbox."""
    def compute() -> str:
        entries: list[tuple[str, str | None]] = []

        inbox_latest_dt = session.scalar(
            select(func.max(Article.date))
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date.is_not(None),
            )
        )
        inbox_latest = (
            inbox_latest_dt.strftime("%Y-%m-%d") if inbox_latest_dt else None
        )
        entries.append((f"{base}/{inbox.name}/", inbox_latest))

        # Distinct (year, month) pairs that actually have data, in
        # one round-trip. Empty months are skipped — they'd 200
        # with a "no messages" page, but the sitemap is for
        # discovery surfaces with real content.
        year_month_rows = session.execute(
            select(
                func.strftime("%Y", Article.date).label("y"),
                func.strftime("%m", Article.date).label("m"),
            )
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date.is_not(None),
            )
            .group_by("y", "m")
            .order_by("y", "m")
        ).all()
        years_with_data: set[str] = {y for y, _ in year_month_rows}
        for y in sorted(years_with_data, reverse=True):
            entries.append((f"{base}/{inbox.name}/{y}/", None))
        for y, m in sorted(year_month_rows, reverse=True):
            entries.append((f"{base}/{inbox.name}/{y}/{m}/", None))

        # Recent articles in the inbox — one URL per article at
        # the inbox's own URL. No canonical-fallback dance:
        # cross-posted articles will appear in each linked
        # inbox's sitemap, which is correct (each is a real,
        # crawlable URL — the canonical `<link>` on the page
        # itself tells search engines which to keep).
        recent = session.execute(
            select(Article.id, Article.date)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date.is_not(None),
            )
            .order_by(Article.date.desc())
            .limit(SITEMAP_RECENT_PER_INBOX)
        ).all()
        for art_id, date in recent:
            entries.append((
                f"{base}/{inbox.name}/{date.year}/{date.month:02d}/{art_id}",
                date.strftime("%Y-%m-%d"),
            ))
        return _build_sitemap_xml(entries)

    return cache.get_or_compute(
        session, f"sitemap:inbox:{inbox.name}", SITEMAP_TTL_SEC, compute,
        force=force,
    )


@bp_web.route("/sitemap.xml")
def sitemap():
    """Sitemap index. Lists `/meta-sitemap.xml` plus one
    `/<inbox>/sitemap.xml` per configured inbox. Crawlers fetch
    sub-sitemaps independently and can skip unchanged inboxes
    between visits via the per-entry `<lastmod>`. Cached for
    SITEMAP_TTL_SEC.

    The split (issue #10) replaced a single monolithic sitemap with
    one global URL cap (1000) and a COALESCE join to pick the
    canonical inbox per cross-posted article. Per-inbox sitemaps
    don't need either — each one lists its own URLs."""
    with SessionLocal() as session:
        body = sitemap_index_xml(session, _site_base())
    return Response(body, mimetype="application/xml; charset=utf-8")


@bp_web.route("/meta-sitemap.xml")
def meta_sitemap():
    """One-entry sitemap listing `/`. Lives behind the sitemap index
    so the index stays purely a `<sitemapindex>` (which can't carry
    a `<url>` for the root directly per sitemaps.org schema). lastmod
    is the global most-recent article date."""
    with SessionLocal() as session:
        body = meta_sitemap_xml(session, _site_base())
    return Response(body, mimetype="application/xml; charset=utf-8")


@bp_web.route("/<inbox_name>/sitemap.xml")
def inbox_sitemap(inbox_name: str):
    """Per-inbox sitemap: dashboard, year + month archives that have
    messages, plus the `SITEMAP_RECENT_PER_INBOX` most-recent article
    URLs. Cached per inbox, so an ingest into one inbox doesn't
    invalidate the others' cached responses."""
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        body = inbox_sitemap_xml(session, inbox, _site_base())
    return Response(body, mimetype="application/xml; charset=utf-8")


# Message-ID lookup. People share Message-IDs in commit trailers,
# bug reports, lore links, IRC; the canonical /<inbox>/<YYYY>/<MM>/
# <article-id> URL requires knowing the date. These two routes
# resolve a bare Message-ID to the canonical URL via 302 with a
# 1-hour cache. 302 (not 301) so a future URL-scheme change isn't
# pinned forever in browser caches.


@bp_web.route("/m/<path:message_id>")
def message_id_lookup(message_id: str):
    """Resolve a bare Message-ID to its canonical URL.

    For cross-posts, redirects directly to the canonical inbox's URL
    (article.canonical_inbox_id; alphabetically-first linked inbox
    when canonical is unset). The message page's "Also in:" line
    surfaces the other inboxes from there. 301 because the target
    is stable for the article's lifetime — transfers link equity
    to the canonical destination.
    """
    with SessionLocal() as session:
        article = session.execute(
            select(Article).where(Article.message_id == message_id)
        ).scalar_one_or_none()
        if article is None:
            abort(404)
        links = list(session.execute(
            select(Inbox.id, Inbox.name)
            .join(ArticleList, ArticleList.inbox_id == Inbox.id)
            .where(ArticleList.article_id == article.id)
        ).all())
        if not links:
            abort(404)
    inbox_name = _canonical_inbox_name(article, links)
    return redirect(_msg_url(article, inbox_name), code=301)


@bp_web.route("/<inbox_name>/m/<path:message_id>")
def message_id_lookup_inbox(inbox_name: str, message_id: str):
    """Inbox-scoped Message-ID lookup. 404 if the message exists but
    isn't linked to this inbox; the unscoped /m/<id> form will find
    it in another inbox if one carries it. Redirects to the
    inbox-scoped article URL (NOT the canonical), preserving the
    user's explicit choice of inbox; the destination page's
    `<link rel="canonical">` still points at the canonical for
    search engines. 301 because the target is stable."""
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        article = session.execute(
            select(Article)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                Article.message_id == message_id,
                ArticleList.inbox_id == inbox.id,
            )
        ).scalar_one_or_none()
        if article is None:
            abort(404)
    return redirect(_msg_url(article, inbox.name), code=301)


@bp_web.route("/")
def index():
    """Meta-index: card grid of configured inboxes with per-inbox
    stats. Pinned inboxes (settings.pinned_inboxes) surface first in
    config order; the rest follow alphabetically.

    Each card carries `archive_stats` (counts + date span) plus a
    30-day `daily_volume` sparkline. Both are cached helpers; the
    per-card cost on a warm cache is one cache row read per inbox.
    """
    pin_rank = {name: i for i, name in enumerate(settings.pinned_inboxes)}
    with SessionLocal() as session:
        inboxes = session.execute(select(Inbox)).scalars().all()
        inboxes.sort(key=lambda ix: (pin_rank.get(ix.name, len(pin_rank)), ix.name))
        inbox_summaries = []
        for inbox in inboxes:
            stats = archive_stats(session, inbox)
            inbox_summaries.append({
                "name": inbox.name,
                "stats": stats,
                "pinned": inbox.name in pin_rank,
                # Relative-time string for the visible "Last activity"
                # line. None when the inbox has no messages yet (the
                # template falls back to the empty-state copy).
                "last_activity_rel": (
                    _relative_time(stats.last_date) if stats.last_date else None
                ),
                # 30-day sparkline. Always renders so cards line up
                # vertically even on dormant inboxes (zero-filled
                # series → flat bar row).
                "spark": daily_volume(session, inbox, days=30),
            })
        # Cross-inbox subsystem teaser. Surfaces the most active
        # subsystems across every configured inbox so a reader on
        # `/` can drill into a hot subsystem without first picking
        # an inbox. Each row carries the inbox where it's busiest.
        active_subsystems = most_active_subsystems_global(
            session, days=7, limit=12,
        )
    base = _site_base()
    return render_template(
        "index.html",
        inbox_summaries=inbox_summaries,
        active_subsystems=active_subsystems,
        current_inbox=None,
        canonical_url=base + "/",
        page_json_ld=_json_ld_index(base, inboxes),
    )


@bp_web.route("/<inbox_name>/")
def inbox_dashboard(inbox_name: str):
    """Per-inbox dashboard: active threads, pulls, releases, trackers,
    history, recent, sparkline, stats."""
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        active = active_threads(session, inbox, days=7, limit=10)
        trackers = [
            {"label": label, "substr": substr, "messages": author_recent(session, inbox, substr, 5)}
            for label, substr in (inbox.tracked_authors or {}).items()
        ]
        pulls = latest_pull_requests(session, inbox, limit=5)
        stable = latest_stable_releases(session, inbox, limit=5)
        history = this_day_in_history(session, inbox, years_ago=5, limit=3)
        recent, recent_has_more = _fetch_recent(session, inbox, 0, RECENT_PAGE_SIZE)
        stats = archive_stats(session, inbox)
        spark = daily_volume(session, inbox, days=30)
        # Subsystem discoverability: top-N most active subsystems in
        # this inbox over the last 7 days. Cached helper, so warm-cache
        # covers steady state. Empty list when no subsystem has
        # supported globs (no MAINTAINERS ingest yet).
        active_subsystems = most_active_subsystems_in_inbox(
            session, inbox, days=7, limit=10,
        )
    base = _site_base()
    year_decades: list[tuple[int, list[int]]] = []
    if stats and stats.first_date and stats.last_date:
        year_decades = _year_decade_groups(
            stats.first_date.year, stats.last_date.year
        )
    return render_template(
        "inbox.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        active=active,
        trackers=trackers,
        pulls=pulls,
        stable=stable,
        history=history,
        recent=recent,
        recent_has_more=recent_has_more,
        recent_next_offset=RECENT_PAGE_SIZE,
        stats=stats,
        spark=spark,
        year_decades=year_decades,
        active_subsystems=active_subsystems,
        canonical_url=f"{base}/{inbox.name}/",
        page_json_ld=_json_ld_inbox(base, inbox, active),
    )


def _daily_view(inbox_name: str, day: date_cls, heading: str):
    """Shared renderer for /<list>/today and /<list>/yesterday."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        threads = threads_for_day(session, inbox, day)
        total = session.scalar(
            select(func.count(Article.id))
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date >= start.strftime("%Y-%m-%d %H:%M:%S"),
                Article.date < end.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
    return render_template(
        "daily.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        day=day,
        heading=heading,
        threads=threads,
        total_messages=total or 0,
    )


@bp_web.route("/<inbox_name>/today")
def daily_today(inbox_name: str):
    today = datetime.now(timezone.utc).date()
    return _daily_view(inbox_name, today, "Today")


@bp_web.route("/<inbox_name>/yesterday")
def daily_yesterday(inbox_name: str):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    return _daily_view(inbox_name, yesterday, "Yesterday")


@bp_web.route("/<inbox_name>/since/<since_str>")
def threads_since_view(inbox_name: str, since_str: str):
    """"What I missed" view: every thread with activity from `since` to
    now. Window is clamped to `THREADS_SINCE_MAX_DAYS` (90 days) below
    the present; the template renders a notice when the requested
    `since` falls before the cap so the operator sees why the window
    starts where it does."""
    try:
        since = date_cls.fromisoformat(since_str)
    except ValueError:
        abort(404)
    today = datetime.now(timezone.utc).date()
    if since > today:
        abort(404)
    floor = today - timedelta(days=THREADS_SINCE_MAX_DAYS)
    capped = since < floor
    effective = floor if capped else since
    start = datetime.combine(effective, datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.now(timezone.utc)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        threads = threads_since(session, inbox, since)
        total = session.scalar(
            select(func.count(Article.id))
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.date >= start.strftime("%Y-%m-%d %H:%M:%S"),
                Article.date < end.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
    return render_template(
        "since.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        since=since,
        effective_since=effective,
        capped=capped,
        max_days=THREADS_SINCE_MAX_DAYS,
        threads=threads,
        total_messages=total or 0,
    )


# Threshold above which the message-page layout switches from
# "thread tree above body" to "thread tree as a right rail" on
# wide viewports. Picked from issue #68's >~20-message guideline:
# below this, the above-body box is fine; above it, the box's
# height cap ends up paginating most of the tree out of view and
# the rail layout (mutt / Thunderbird / Discourse) is what
# everyone expects. The CSS still falls back to above-body on
# narrow viewports regardless of length.
LONG_THREAD_SIDEBAR_THRESHOLD = 20


# Cap on the per-subsystem dashboard's recent-patches list.
# MAINTAINERS subsystems vary wildly in volume (BCACHEFS is busy,
# some are dormant). A flat cap keeps response size bounded and the
# rendered list readable; a future slice can paginate.
SUBSYSTEM_RECENT_PATCHES_LIMIT = 30


@bp_web.route("/<inbox_name>/subsystem/<path:name>/")
def subsystem_dashboard(inbox_name: str, name: str):
    """Per-subsystem dashboard. Surfaces the MAINTAINERS-derived
    header (name, status, M:/R: maintainers), F:/X: paths, a 30-day
    sparkline, recent patches, active threads, and active reviewers
    for articles whose diff-touched paths match the subsystem's
    globs (minus X: vetoes).

    URL is lowercased by convention. MAINTAINERS stores names in
    upper-case ASCII ("BCACHEFS"), which is fine for the operator-
    facing dashboard heading but produces shouty URLs in bookmarks
    and browser history. The route accepts any casing,
    case-insensitive-matches against `subsystems.name`, and 301s
    non-canonical (uppercase) requests to the canonical lowercase
    form. The DB row's stored casing remains the upstream-verbatim
    one and is what the H1 renders.
    """
    name_lower = name.lower()
    if name != name_lower:
        return redirect(
            f"/{inbox_name}/subsystem/{name_lower}/", code=301,
        )
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        subsystem = session.execute(
            select(Subsystem)
            .options(
                selectinload(Subsystem.maintainers),
                selectinload(Subsystem.paths),
            )
            .where(func.lower(Subsystem.name) == name_lower)
        ).scalar_one_or_none()
        if subsystem is None:
            abort(404)
        recent = recent_articles_in_subsystem(
            session, inbox, subsystem,
            limit=SUBSYSTEM_RECENT_PATCHES_LIMIT,
        )
        active = active_threads_in_subsystem(
            session, inbox, subsystem, days=7, limit=10,
        )
        spark = daily_volume_in_subsystem(
            session, inbox, subsystem, days=30,
        )
        reviewers = active_reviewers_in_subsystem(
            session, inbox, subsystem, days=30, limit=10,
        )
    return render_template(
        "subsystem.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        subsystem=subsystem,
        recent=recent,
        recent_limit=SUBSYSTEM_RECENT_PATCHES_LIMIT,
        active=active,
        spark=spark,
        reviewers=reviewers,
    )


# Plausible bounds for an inbox archive: lkml itself goes back to ~1995.
# Outside this range a year URL is almost certainly user error / scraper
# noise; 404 is the right response.
_MIN_ARCHIVE_YEAR = 1995

# Cap for the month-archive thread list. A busy month on lkml has
# thousands of threads; rendering all of them blows the response past
# 1 MB. The view shows the most-recent N + the total-message count
# from `monthly_volume` so context isn't lost.
MONTH_THREAD_CAP = 100

# Search input bounds. The query string flows into a cache key, so a
# soft length cap keeps the cache bounded and makes DoS-via-arbitrary-
# queries less interesting. Min length avoids matching the entire
# corpus on a single character.
SEARCH_QUERY_MIN_LEN = 2
SEARCH_QUERY_MAX_LEN = 80
SEARCH_RESULT_CAP = 100


def _max_archive_year() -> int:
    return datetime.now(timezone.utc).year + 1


@bp_web.route("/<inbox_name>/<int:year>/")
def year_archive(inbox_name: str, year: int):
    """Year view for an inbox: 12 month cells with per-month message
    counts, cells link to the month view."""
    if year < _MIN_ARCHIVE_YEAR or year > _max_archive_year():
        abort(404)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        volume = monthly_volume(session, inbox, year)
    return render_template(
        "year.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        year=year,
        volume=volume,
        prev_year=year - 1 if year - 1 >= _MIN_ARCHIVE_YEAR else None,
        next_year=year + 1 if year + 1 <= _max_archive_year() else None,
    )


@bp_web.route("/<inbox_name>/<int:year>/<int:month>/")
def month_archive(inbox_name: str, year: int, month: int):
    """Month view for an inbox: every thread with at least one
    message in (year, month), ordered by last activity desc."""
    if year < _MIN_ARCHIVE_YEAR or year > _max_archive_year():
        abort(404)
    if month < 1 or month > 12:
        abort(404)
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        threads = threads_for_month(session, inbox, year, month, limit=MONTH_THREAD_CAP)
        # Reuse the cached `monthly_volume` count — keeps the warm-
        # path off the COUNT(*) over the month's article rows.
        volume = monthly_volume(session, inbox, year)
        total = next((c for m, c in volume.months if m == month), 0)

    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    return render_template(
        "month.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        year=year,
        month=month,
        month_label=start.strftime("%B %Y"),
        threads=threads,
        total_messages=total or 0,
        thread_cap=MONTH_THREAD_CAP,
        prev_url=(
            f"/{inbox.name}/{prev_year:04d}/{prev_month:02d}/"
            if prev_year >= _MIN_ARCHIVE_YEAR else None
        ),
        next_url=(
            f"/{inbox.name}/{next_year:04d}/{next_month:02d}/"
            if next_year <= _max_archive_year() else None
        ),
    )


@bp_web.route("/<inbox_name>/search")
def search(inbox_name: str):
    """Substring search over subject and author within one inbox.
    GET ?q=<query>; renders the search form even on no-result so the
    user always lands on a page with the input."""
    raw_q = request.args.get("q", "").strip()
    q = raw_q[:SEARCH_QUERY_MAX_LEN]

    too_short = 0 < len(q) < SEARCH_QUERY_MIN_LEN
    results: list = []
    truncated = False
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        if len(q) >= SEARCH_QUERY_MIN_LEN:
            results = search_articles(session, inbox, q, limit=SEARCH_RESULT_CAP)
            truncated = len(results) >= SEARCH_RESULT_CAP

    # SearchResultsPage only when we're actually rendering results.
    # The no-query and too-short shapes are a bare search form, not a
    # "results page" — emitting the type would give crawlers a wrong
    # signal. Canonical URL is `_site_base() + /<inbox>/search` (no
    # query), matching the <link rel="canonical"> the context
    # processor emits — keeps individual ?q= URLs out of the index.
    page_json_ld = None
    if results:
        canonical_url = _site_base() + f"/{inbox.name}/search"
        page_json_ld = _json_ld_search(
            _site_base(), inbox.name, q, canonical_url,
        )

    return render_template(
        "search.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        query=q,
        results=results,
        truncated=truncated,
        result_cap=SEARCH_RESULT_CAP,
        too_short=too_short,
        min_len=SEARCH_QUERY_MIN_LEN,
        max_len=SEARCH_QUERY_MAX_LEN,
        page_json_ld=page_json_ld,
    )


FEED_ENTRY_LIMIT = 50

# Tag-URI host portion (RFC 4151). Constant rather than the request
# host so feed entry IDs stay stable across re-deployments and aren't
# tied to whatever proxy the request came in through.
_TAG_URI_AUTHORITY = "mimir"


def _atom_response(
    *,
    feed_id: str,
    feed_title: str,
    self_url: str,
    alternate_url: str,
    entries: list[ArticleSummary],
    inbox_name: str,
    base_url: str,
    canonical_inbox_by_article: dict[int, str] | None = None,
) -> Response:
    """Render an Atom 1.0 feed from a list of `ArticleSummary`. Uses
    stdlib ElementTree — no extra dep. Emits redacted authors via the
    same `safe_from` rule the HTML side uses, so private email
    addresses don't leak via feed readers either.

    `canonical_inbox_by_article` maps article.id → canonical inbox name.
    Cross-posts get their `<id>` and `<link>` set to the canonical URL
    so feed readers that key on `<id>` deduplicate across feeds (the
    same article appearing in lkml's and linux-fsdevel's feeds renders
    as one entry, not two)."""
    feed_updated = (
        max((e.date for e in entries if e.date), default=None)
        or datetime.now(timezone.utc)
    )
    canonical_map = canonical_inbox_by_article or {}

    feed = Element("feed", xmlns="http://www.w3.org/2005/Atom")
    SubElement(feed, "id").text = feed_id
    SubElement(feed, "title").text = feed_title
    SubElement(feed, "updated").text = feed_updated.strftime("%Y-%m-%dT%H:%M:%SZ")
    SubElement(feed, "link", rel="self", type="application/atom+xml", href=self_url)
    SubElement(feed, "link", rel="alternate", type="text/html", href=alternate_url)
    SubElement(feed, "generator").text = "mimir"

    msg_base = base_url.rstrip("/")
    for a in entries:
        entry = SubElement(feed, "entry")
        # RFC 4151 tag URI. Use the canonical inbox name in the tag so
        # cross-posted entries collapse to a single id across feeds —
        # readers that key on <id> won't show duplicates.
        date_str = a.date.strftime("%Y-%m-%d") if a.date else "1970-01-01"
        canonical_inbox_name = canonical_map.get(a.id, inbox_name)
        SubElement(entry, "id").text = (
            f"tag:{_TAG_URI_AUTHORITY},{date_str}:{canonical_inbox_name}/{a.id}"
        )
        SubElement(entry, "title").text = a.subject or "(no subject)"
        if a.date is not None:
            SubElement(entry, "updated").text = a.date.strftime("%Y-%m-%dT%H:%M:%SZ")
        SubElement(
            entry, "link",
            rel="alternate", type="text/html",
            href=msg_base + _msg_url(a, canonical_inbox_name),
        )
        if a.author:
            author_el = SubElement(entry, "author")
            # Display name only — same posture as JSON-LD's author.name.
            # Feed readers render <author><name> as the byline; the
            # `<hidden>` placeholder reads as broken metadata there
            # exactly as it did in JSON-LD before the 2026-05-12 fix.
            SubElement(author_el, "name").text = _display_name_filter(a.author)

    body = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        + tostring(feed, encoding="unicode")
    )
    return Response(body, mimetype="application/atom+xml; charset=utf-8")


AUTHOR_VIEW_LIMIT = 100


@bp_web.route("/<inbox_name>/author/<sub>")
def author_view(inbox_name: str, sub: str):
    """Chronological view of recent messages from one author within an
    inbox. Reuses `author_recent` (cached); shows up to
    AUTHOR_VIEW_LIMIT most-recent matches."""
    sub = sub.strip()[:SEARCH_QUERY_MAX_LEN]
    if len(sub) < SEARCH_QUERY_MIN_LEN:
        abort(404)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        results = author_recent(session, inbox, sub, limit=AUTHOR_VIEW_LIMIT)
    # Pin the canonical URL with `sub` percent-encoded so the page's
    # <link rel="canonical"> matches the <link rel="alternate"> for
    # the atom feed (which uses Jinja's `urlencode` filter on `sub`).
    # `request.path` would surface raw `@` for queries like
    # `torvalds@`; the encoded form is standards-conformant and keeps
    # the two surfaces consistent (2026-05-13 review nit).
    base = _site_base()
    sub_quoted = quote(sub, safe="")
    canonical_url = f"{base}/{inbox.name}/author/{sub_quoted}"
    return render_template(
        "author.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        sub=sub,
        results=results,
        truncated=len(results) >= AUTHOR_VIEW_LIMIT,
        result_cap=AUTHOR_VIEW_LIMIT,
        canonical_url=canonical_url,
        page_json_ld=_json_ld_author(base, inbox.name, sub, canonical_url),
    )


# Conservative pattern for the URL-side address: the same shape the
# trailer extractor accepts (mimir/trailers.py _TRAILER_NAME_ADDR_RE).
# Anything outside this falls to 404 — defends against hostile bytes
# reaching the SQL parameter and keeps the canonical URL well-formed.
_REVIEWER_ADDR_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+$"
)


@bp_web.route("/<inbox_name>/reviewer/<path:address>")
def reviewer_view(inbox_name: str, address: str):
    """Per-reviewer attestation listing: every patch in `inbox_name`
    where `address` appears on a `Reviewed-by` / `Acked-by` /
    `Tested-by` / `Reported-by` / `Suggested-by` /
    `Co-developed-by` / `Reported-and-tested-by` trailer.

    The address from the URL is lowercased to match the
    `address_normalized` index column. We accept any well-formed
    address in the URL (the route is a public navigation surface),
    but outbound links to this surface are only generated from
    allowlisted addresses (see `_is_allowlisted_address_filter`).
    Non-allowlisted addresses can be navigated to directly by anyone
    who already knows them, matching the existing posture that
    redaction is friction, not a privacy guarantee.
    """
    if not _REVIEWER_ADDR_RE.match(address):
        abort(404)
    address_normalized = address.lower()
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        entries = articles_reviewed_by(
            session, inbox, address_normalized,
            limit=REVIEWS_PER_PAGE_LIMIT,
        )
    role_counts: dict[str, int] = {}
    for e in entries:
        role_counts[e.role] = role_counts.get(e.role, 0) + 1
    base = _site_base()
    canonical_url = f"{base}/{inbox.name}/reviewer/{quote(address_normalized, safe='@')}"
    return render_template(
        "reviewer.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        address=address_normalized,
        entries=entries,
        role_counts=role_counts,
        total=len(entries),
        truncated=len(entries) >= REVIEWS_PER_PAGE_LIMIT,
        result_cap=REVIEWS_PER_PAGE_LIMIT,
        canonical_url=canonical_url,
    )


def _canonical_inbox_names_for(
    session: Session, article_ids: list[int],
) -> dict[int, str]:
    """Resolve article_id → canonical inbox name for a batch (typically
    a feed's worth, ≤50). Falls back to the alphabetically-first linked
    inbox when canonical_inbox_id is NULL. One query for the canonical
    side, one for the fallback set; total ≤2 queries regardless of
    batch size."""
    if not article_ids:
        return {}
    out: dict[int, str] = {}
    rows = session.execute(
        select(Article.id, Inbox.name)
        .join(Inbox, Article.canonical_inbox_id == Inbox.id)
        .where(Article.id.in_(article_ids))
    ).all()
    for art_id, inbox_name in rows:
        out[art_id] = inbox_name
    missing = [aid for aid in article_ids if aid not in out]
    if missing:
        fallback = session.execute(
            select(ArticleList.article_id, func.min(Inbox.name))
            .join(Inbox, Inbox.id == ArticleList.inbox_id)
            .where(ArticleList.article_id.in_(missing))
            .group_by(ArticleList.article_id)
        ).all()
        for art_id, inbox_name in fallback:
            out[art_id] = inbox_name
    return out


@bp_web.route("/<inbox_name>/feed.atom")
def inbox_feed(inbox_name: str):
    """Atom feed of the most-recent messages in an inbox."""
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        entries = recent_articles(session, inbox, limit=FEED_ENTRY_LIMIT)
        canonical_map = _canonical_inbox_names_for(session, [e.id for e in entries])

    base = _site_base() + "/"
    return _atom_response(
        feed_id=f"{base}{inbox.name}/feed.atom",
        feed_title=f"{inbox.name} | {settings.site_name}",
        self_url=f"{base}{inbox.name}/feed.atom",
        alternate_url=f"{base}{inbox.name}/",
        entries=entries,
        inbox_name=inbox.name,
        base_url=base,
        canonical_inbox_by_article=canonical_map,
    )


@bp_web.route("/<inbox_name>/author/<sub>/feed.atom")
def author_feed(inbox_name: str, sub: str):
    """Atom feed of recent messages from one author. `sub` is a
    substring of From — same shape the dashboard tracker uses, scoped
    to one inbox."""
    sub = sub.strip()[:SEARCH_QUERY_MAX_LEN]
    if len(sub) < SEARCH_QUERY_MIN_LEN:
        abort(404)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        entries = author_recent(session, inbox, sub, limit=FEED_ENTRY_LIMIT)
        canonical_map = _canonical_inbox_names_for(session, [e.id for e in entries])

    base = _site_base() + "/"
    sub_quoted = quote(sub, safe="")
    return _atom_response(
        feed_id=f"{base}{inbox.name}/author/{sub_quoted}/feed.atom",
        feed_title=f"{sub} on {inbox.name} | {settings.site_name}",
        self_url=f"{base}{inbox.name}/author/{sub_quoted}/feed.atom",
        alternate_url=f"{base}{inbox.name}/",
        entries=entries,
        inbox_name=inbox.name,
        base_url=base,
        canonical_inbox_by_article=canonical_map,
    )


@bp_web.route("/api/<inbox_name>/recent")
def api_recent(inbox_name: str):
    """HTMX load-more endpoint for the Recent messages list, scoped to
    one inbox. Returns the `_recent_items.html` partial: the next page
    of <li>s plus a fresh 'Load more' trigger (or nothing, if exhausted)."""
    offset = max(0, request.args.get("offset", default=0, type=int))
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        recent, recent_has_more = _fetch_recent(session, inbox, offset, RECENT_PAGE_SIZE)
    return render_template(
        "_recent_items.html",
        inbox_name=inbox.name,
        recent=recent,
        recent_has_more=recent_has_more,
        recent_next_offset=offset + RECENT_PAGE_SIZE,
    )


def _fetch_article_for_attachment(
    session: Session,
    inbox: Inbox,
    year: int,
    month: int,
    article_id: int,
    n: int,
):
    """Validate URL parts + fetch the n-th attachment via read_message.
    Used by both download and preview routes; aborts 404 on any mismatch."""
    article = session.get(Article, article_id)
    if article is None:
        abort(404)
    # Article must be linked to the URL's inbox (cross-posts have one row per inbox).
    linked = session.execute(
        select(ArticleList.article_id).where(
            ArticleList.article_id == article_id,
            ArticleList.inbox_id == inbox.id,
        )
    ).scalar_one_or_none()
    if linked is None:
        abort(404)
    if article.date is None or year != article.date.year or month != article.date.month:
        abort(404)
    try:
        parsed = read_message(session, inbox, article.message_id)
    except MessageNotFound:
        abort(404)
    if n < 0 or n >= len(parsed.attachments):
        abort(404)
    return article, parsed.attachments[n]


_HEADER_CTL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _content_disposition(filename: str | None) -> str:
    """Build a Content-Disposition header that handles non-ASCII filenames
    via RFC 6266 (filename* parameter).

    Strips control bytes (CR, LF, NUL, tab, DEL) from the ASCII
    `filename="…"` form: a maliciously-crafted attachment filename
    carrying CR/LF would otherwise inject extra HTTP response
    headers (RFC 7230 header-line splitting). Defense in depth on
    top of whatever the WSGI layer rejects. The percent-encoded
    `filename*` form is unaffected — `quote()` already escapes
    them as `%0D`/`%0A`/etc.
    """
    if not filename:
        return "attachment"
    safe_ascii = _HEADER_CTL_RE.sub("", filename)
    safe_ascii = safe_ascii.replace('"', "").replace("\\", "")
    quoted = quote(filename, safe="")
    return f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{quoted}'


@bp_web.route(
    "/<inbox_name>/<int:year>/<int:month>/<int:article_id>/attachment/<int:n>"
)
def attachment_download(inbox_name: str, year: int, month: int, article_id: int, n: int):
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        _, att = _fetch_article_for_attachment(
            session, inbox, year, month, article_id, n
        )
    return Response(
        att.content,
        mimetype=att.content_type or "application/octet-stream",
        headers={"Content-Disposition": _content_disposition(att.filename)},
    )


@bp_web.route(
    "/<inbox_name>/<int:year>/<int:month>/<int:article_id>/attachment/<int:n>/preview"
)
def attachment_preview(inbox_name: str, year: int, month: int, article_id: int, n: int):
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        article, att = _fetch_article_for_attachment(
            session, inbox, year, month, article_id, n
        )
    if not _is_previewable(att):
        return render_template(
            "attachment_preview.html",
            inbox_name=inbox.name, current_inbox=inbox.name,
            article=article, att=att, n=n,
            previewable=False,
        )
    text_content = att.content.decode("utf-8", errors="replace")
    lexer = _lexer_for(att.filename, text_content)
    formatter = HtmlFormatter(
        noclasses=True, nobackground=True, style="default", linenos="inline",
    )
    highlighted = highlight(text_content, lexer, formatter)
    return render_template(
        "attachment_preview.html",
        inbox_name=inbox.name, current_inbox=inbox.name,
        article=article, att=att, n=n,
        previewable=True,
        highlighted=highlighted,
        lexer_name=lexer.name,
    )


@bp_web.route("/<inbox_name>/<int:year>/<int:month>/<int:article_id>")
def message(inbox_name: str, year: int, month: int, article_id: int):
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        article = session.get(Article, article_id)
        if article is None:
            abort(404)
        # Article must be linked to this inbox (cross-posts get one row per inbox).
        linked = session.execute(
            select(ArticleList.article_id).where(
                ArticleList.article_id == article_id,
                ArticleList.inbox_id == inbox.id,
            )
        ).scalar_one_or_none()
        if linked is None:
            abort(404)

        # The URL date is part of the message's identity; reject mismatches
        # so URLs are a reliable age-gauge.
        if article.date is None or year != article.date.year or month != article.date.month:
            abort(404)

        try:
            parsed = read_message(session, inbox, article.message_id)
        except MessageNotFound:
            abort(404)

        # Full thread context (replaces the v1 parent + immediate-replies view).
        # Walk constrained to this article's inbox.
        root_msgid = find_thread_root(session, inbox, article.message_id) or article.message_id
        thread = get_thread(session, inbox, root_msgid)

        # If the thread root still has a thread_parent (i.e. our walk-up hit
        # the top of what we have, but the original chain continued through
        # a message not in this archive), surface that as an "off-list"
        # hint in the UI. Common for cross-posted threads from oss-security,
        # linux-arm-kernel, etc.
        parent_off_list: str | None = None
        if thread and thread[0].thread_parent:
            in_db = session.execute(
                select(Article.id)
                .join(ArticleList, ArticleList.article_id == Article.id)
                .where(
                    ArticleList.inbox_id == inbox.id,
                    Article.message_id == thread[0].thread_parent,
                )
            ).scalar_one_or_none()
            if in_db is None:
                parent_off_list = thread[0].thread_parent

        # When the parent is off-list, surface To/Cc list-shaped addresses
        # that don't map to any configured inbox as a "hint" — those are
        # candidate mailing lists the operator might want to add to mimir
        # to recover the missing parent. Strictly a hint: the parent could
        # also simply predate the indexed window. Capped at 3 to avoid a
        # wall of addresses on heavily cross-posted threads.
        parent_off_list_hints: list[str] = []
        if parent_off_list:
            candidates = extract_list_addresses(parsed.headers)
            if candidates:
                known = {
                    addr.lower()
                    for (addr,) in session.execute(
                        select(Inbox.list_address).where(Inbox.list_address.is_not(None))
                    ).all()
                    if addr
                }
                parent_off_list_hints = [a for a in candidates if a not in known][:3]

        # Same-subject orphans within this inbox.
        related: list[Article] = []
        if parent_off_list and article.subject_normalized:
            in_thread = {n.message_id for n in thread}
            related = list(
                session.execute(
                    select(Article)
                    .join(ArticleList, ArticleList.article_id == Article.id)
                    .where(
                        ArticleList.inbox_id == inbox.id,
                        Article.subject_normalized == article.subject_normalized,
                        Article.message_id.not_in(in_thread),
                    )
                    .order_by(Article.date.desc().nulls_last())
                    .limit(5)
                ).scalars()
            )

        # Build URLs for thread nodes (avoids per-node template logic).
        thread_urls = {
            n.message_id: f"/{inbox.name}/{n.date.year}/{n.date.month:02d}/{n.id}"
            if n.date
            else f"/{inbox.name}/0000/00/{n.id}"
            for n in thread
        }

        # Resolve in-body <Message-ID> references to canonical URLs.
        # Restrict to this inbox — cross-list refs render as plain text.
        msgid_urls: dict[str, str] = {}
        if parsed.body:
            candidates = {
                m.group("msgid")
                for m in URL_OR_MSGID_RE.finditer(parsed.body)
                if m.group("msgid")
            }
            if candidates:
                referenced = session.execute(
                    select(Article)
                    .join(ArticleList, ArticleList.article_id == Article.id)
                    .where(
                        ArticleList.inbox_id == inbox.id,
                        Article.message_id.in_(candidates),
                    )
                ).scalars().all()
                msgid_urls = {a.message_id: _msg_url(a, inbox.name) for a in referenced}

        # All inboxes this article is linked to. Used for both the
        # cross-post hint (which excludes the current inbox) and the
        # canonical URL (which picks one across the full set).
        all_links: list[tuple[int, str]] = list(session.execute(
            select(Inbox.id, Inbox.name)
            .join(ArticleList, ArticleList.inbox_id == Inbox.id)
            .where(ArticleList.article_id == article.id)
            .order_by(Inbox.name)
        ).all())
        cross_post_inboxes = [n for ix_id, n in all_links if ix_id != inbox.id]
        base = _site_base()
        canonical_url = _canonical_url_for(article, all_links, base=base)
        # Canonical inbox is what JSON-LD's isPartOf and the breadcrumb
        # should reflect — not necessarily the current URL's inbox.
        canonical_inbox_name = (
            _canonical_inbox_name(article, all_links) or inbox.name
        )
        page_json_ld = _json_ld_message(
            article, parsed, canonical_url or "", canonical_inbox_name, base,
        ) if canonical_url else None

    # Summary line for the closed-state fold ("23 messages, 5 authors, 2h ago").
    thread_summary = _thread_summary(thread)

    # Subsystem header + related patches + mainline applications +
    # patch-series timeline. Single session for all lookups; empty
    # results are handled template-side via `{% if %}`. Opens a
    # fresh session since the route's earlier with-block has closed
    # by this point.
    with SessionLocal() as session:
        subsystem_hits = subsystems_for_article(session, article.id)
        touched_paths = [
            f.path for f in session.execute(
                select(ArticleFile).where(ArticleFile.article_id == article.id)
            ).scalars()
        ]
        related_patches = recent_patches_touching(
            session, touched_paths, exclude_article_id=article.id, limit=5,
        ) if touched_paths else []
        mainline_applications = list(session.execute(
            select(MainlineCommit)
            .where(MainlineCommit.message_id == article.message_id)
            .order_by(MainlineCommit.committed_at.asc())
        ).scalars())
        patch_series_revisions: list[tuple[Article, str]] = []
        if article.patch_series_key:
            revisions = list(session.execute(
                select(Article)
                .options(selectinload(Article.lists))
                .where(Article.patch_series_key == article.patch_series_key)
                .order_by(Article.date.asc().nulls_last())
            ).scalars())
            for rev in revisions:
                link_set = [(al.inbox_id, al.inbox.name) for al in rev.lists]
                url = _canonical_url_for(rev, link_set, base="") or ""
                patch_series_revisions.append((rev, url))

    # Parent URL for the hunk-anchored quote renderer: the message
    # this article replies to. When the parent is in-scope (i.e. in
    # the same archive and surfaced in the thread tree), `thread_urls`
    # carries its URL. Off-list parents have no URL and the renderer
    # falls back to a plain <details> without the jump link.
    parent_url: str | None = None
    if article.thread_parent and article.thread_parent in thread_urls:
        parent_url = thread_urls[article.thread_parent]

    # Long threads switch to a right-rail tree layout on wide
    # viewports — see LONG_THREAD_SIDEBAR_THRESHOLD. Narrow
    # viewports stack regardless via the CSS media query.
    long_thread = len(thread) >= LONG_THREAD_SIDEBAR_THRESHOLD

    # HTMX intra-thread swap: when the click came from a tree link, return
    # only the message-body partial (just the <article id="msg">). The
    # surrounding tree + nav stay put on the client; the client-side script
    # flips which <li> carries the .is-active class after the swap.
    template = (
        "_message_body.html"
        if request.headers.get("HX-Request") == "true"
        else "message.html"
    )
    return render_template(
        template,
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        parsed=parsed,
        article=article,
        thread=thread,
        thread_urls=thread_urls,
        parent_url=parent_url,
        thread_summary=thread_summary,
        msgid_urls=msgid_urls,
        parent_off_list=parent_off_list,
        parent_off_list_hints=parent_off_list_hints,
        related=related,
        cross_post_inboxes=cross_post_inboxes,
        canonical_url=canonical_url,
        page_json_ld=page_json_ld,
        subsystem_hits=subsystem_hits,
        related_patches=related_patches,
        mainline_applications=mainline_applications,
        patch_series_revisions=patch_series_revisions,
        long_thread=long_thread,
    )
