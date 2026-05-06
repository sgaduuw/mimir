import json
import logging
import secrets
import time
from datetime import date as date_cls, datetime, timedelta, timezone
from email.utils import parseaddr
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, tostring

from flask import Blueprint, Response, abort, g, redirect, render_template, request
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename, guess_lexer
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from mimir import cache
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
from mimir.models import Article, ArticleList, Inbox
from mimir.rendering import URL_OR_MSGID_RE, render_body
from mimir.store import MessageNotFound, read_message
from mimir.threading import (
    active_threads,
    find_thread_root,
    get_thread,
    threads_for_day,
    threads_for_month,
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
    the configurable brand; "mimir" stays as the page generator."""
    from mimir import __version__ as mimir_version
    return {
        "inboxes": inbox_names(),
        "site_name": settings.site_name,
        "mimir_version": mimir_version,
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
    "web.year_archive": "public, max-age=600",
    "web.month_archive": "public, max-age=600",
    "web.search": "public, max-age=300",
    "web.author_view": "public, max-age=300",
    "web.inbox_feed": "public, max-age=300",
    "web.author_feed": "public, max-age=300",
    "web.robots": "public, max-age=86400",
    "web.security_txt": "public, max-age=3600",
    "web.sitemap": "public, max-age=300",
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
    # 302 redirects (Message-ID lookup) are equally cacheable; honor
    # their dict entry too. 4xx/5xx skip — error responses shouldn't
    # be pinned in upstream caches.
    if response.status_code in (200, 302):
        rule = _CACHE_CONTROL_BY_ENDPOINT.get(request.endpoint)
        if rule:
            response.headers["Cache-Control"] = rule
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


@bp_web.app_template_filter("msg_url")
def _msg_url_filter(article: Article, inbox_name: str) -> str:
    return _msg_url(article, inbox_name)


@bp_web.app_template_filter("render_body")
def _render_body_filter(body, msgid_urls=None):
    return render_body(body, msgid_urls=msgid_urls)


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


@bp_web.app_template_filter("safe_from")
def _safe_from_filter(author: str | None) -> str:
    """Return a From-line suitable for display: full address for senders in
    `settings.email_allowlist`, otherwise display name + `<hidden>` to keep
    casual senders' addresses out of the archive UI."""
    if not author:
        return ""
    name, addr = parseaddr(author)
    if not addr:
        return author  # unparseable; show as-is
    addr_lower = addr.lower()
    if any(token.lower() in addr_lower for token in settings.email_allowlist):
        return author
    if name:
        return f"{name} <hidden>"
    return "<hidden>"


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
    sitemap_url = request.url_root.rstrip("/") + "/sitemap.xml"
    body = render_template("robots.txt", sitemap_url=sitemap_url)
    return Response(body, mimetype="text/plain; charset=utf-8")


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


SITEMAP_RECENT_GLOBAL = 1000
SITEMAP_TTL_SEC = 3600


def _build_sitemap_xml(urls: list[str]) -> str:
    """Render an XML sitemap (<urlset> with one <url><loc> per entry)
    via stdlib ElementTree, matching `_atom_response`'s idiom."""
    root = Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for u in urls:
        url_el = SubElement(root, "url")
        SubElement(url_el, "loc").text = u
    return '<?xml version="1.0" encoding="utf-8"?>\n' + tostring(root, encoding="unicode")


@bp_web.route("/sitemap.xml")
def sitemap():
    """Sitemap: meta-index, per-inbox dashboards, and the global
    most-recent N articles, each at exactly one URL — the canonical
    inbox's URL for the article, falling back to the alphabetically-
    first linked inbox when canonical_inbox_id is NULL. One URL per
    article means crawlers don't see the same content under multiple
    URLs (the duplicate-content trap that justified Phases 1–3).
    Cached for SITEMAP_TTL_SEC."""
    base = request.url_root.rstrip("/")
    with SessionLocal() as session:
        def compute() -> str:
            urls: list[str] = [base + "/"]
            inboxes = session.execute(select(Inbox).order_by(Inbox.name)).scalars().all()
            for inbox in inboxes:
                urls.append(f"{base}/{inbox.name}/")

            # COALESCE(canonical_inbox.name, alphabetical-first-linked-name).
            # The fallback subquery handles articles whose To/Cc didn't
            # name a known list — keeps the sitemap deterministic
            # without introducing a NULL-canonical bucket.
            canonical_alias = aliased(Inbox)
            fallback_name = (
                select(func.min(Inbox.name))
                .join(ArticleList, ArticleList.inbox_id == Inbox.id)
                .where(ArticleList.article_id == Article.id)
                .correlate(Article)
                .scalar_subquery()
            )
            inbox_name_expr = func.coalesce(canonical_alias.name, fallback_name)
            recent = session.execute(
                select(Article.id, Article.date, inbox_name_expr.label("inbox_name"))
                .outerjoin(canonical_alias, Article.canonical_inbox_id == canonical_alias.id)
                .where(Article.date.is_not(None))
                .order_by(Article.date.desc())
                .limit(SITEMAP_RECENT_GLOBAL)
            ).all()
            for art_id, date, inbox_name in recent:
                if inbox_name is None:
                    continue  # corrupt row with no links; skip rather than crash
                urls.append(
                    f"{base}/{inbox_name}/{date.year}/{date.month:02d}/{art_id}"
                )
            return _build_sitemap_xml(urls)
        body = cache.get_or_compute(session, "sitemap:root", SITEMAP_TTL_SEC, compute)
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

    For cross-posts, redirects to the alphabetically-first inbox
    that carries the message; the message page's "Also in:" line
    surfaces the other inboxes from there.
    """
    with SessionLocal() as session:
        row = session.execute(
            select(Article, Inbox.name)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .join(Inbox, Inbox.id == ArticleList.inbox_id)
            .where(Article.message_id == message_id)
            .order_by(Inbox.name)
            .limit(1)
        ).first()
        if row is None:
            abort(404)
        article, inbox_name = row
    return redirect(_msg_url(article, inbox_name), code=302)


@bp_web.route("/<inbox_name>/m/<path:message_id>")
def message_id_lookup_inbox(inbox_name: str, message_id: str):
    """Inbox-scoped Message-ID lookup. 404 if the message exists but
    isn't linked to this inbox; the unscoped /m/<id> form will find
    it in another inbox if one carries it."""
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
    return redirect(_msg_url(article, inbox.name), code=302)


@bp_web.route("/")
def index():
    """Meta-index: list of configured inboxes with per-inbox stats.
    Pinned inboxes (settings.pinned_inboxes) surface first in config
    order; the rest follow alphabetically."""
    pin_rank = {name: i for i, name in enumerate(settings.pinned_inboxes)}
    with SessionLocal() as session:
        inboxes = session.execute(select(Inbox)).scalars().all()
        inboxes.sort(key=lambda ix: (pin_rank.get(ix.name, len(pin_rank)), ix.name))
        inbox_summaries = [
            {"name": inbox.name, "stats": archive_stats(session, inbox)}
            for inbox in inboxes
        ]
    return render_template(
        "index.html",
        inbox_summaries=inbox_summaries,
        current_inbox=None,
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
            for label, substr in settings.tracked_authors.items()
        ]
        pulls = latest_pull_requests(session, inbox, limit=5)
        stable = latest_stable_releases(session, inbox, limit=5)
        history = this_day_in_history(session, inbox, years_ago=5, limit=3)
        recent, recent_has_more = _fetch_recent(session, inbox, 0, RECENT_PAGE_SIZE)
        stats = archive_stats(session, inbox)
        spark = daily_volume(session, inbox, days=30)
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
            SubElement(author_el, "name").text = _safe_from_filter(a.author)

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
    return render_template(
        "author.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        sub=sub,
        results=results,
        truncated=len(results) >= AUTHOR_VIEW_LIMIT,
        result_cap=AUTHOR_VIEW_LIMIT,
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

    base = request.url_root
    return _atom_response(
        feed_id=f"{base}{inbox.name}/feed.atom",
        feed_title=f"{inbox.name} · {settings.site_name}",
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

    base = request.url_root
    sub_quoted = quote(sub, safe="")
    return _atom_response(
        feed_id=f"{base}{inbox.name}/author/{sub_quoted}/feed.atom",
        feed_title=f"{sub} on {inbox.name} · {settings.site_name}",
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


def _content_disposition(filename: str | None) -> str:
    """Build a Content-Disposition header that handles non-ASCII filenames
    via RFC 6266 (filename* parameter)."""
    if not filename:
        return "attachment"
    safe_ascii = filename.replace('"', "").replace("\\", "")
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
        canonical_url = _canonical_url_for(
            article, all_links, base=request.url_root.rstrip("/"),
        )

    return render_template(
        "message.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        parsed=parsed,
        article=article,
        thread=thread,
        thread_urls=thread_urls,
        msgid_urls=msgid_urls,
        parent_off_list=parent_off_list,
        related=related,
        cross_post_inboxes=cross_post_inboxes,
        canonical_url=canonical_url,
    )
