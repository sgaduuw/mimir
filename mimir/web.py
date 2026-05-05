from datetime import date as date_cls, datetime, timedelta, timezone
from email.utils import parseaddr
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, tostring

from flask import Blueprint, Response, abort, render_template, request
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename, guess_lexer
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound
from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
    return {"inboxes": inbox_names(), "site_name": settings.site_name}


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


@bp_web.after_request
def _add_cache_headers(response):
    if response.status_code == 200:
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
    return response


def _msg_url(article: Article, inbox_name: str) -> str:
    """Build the canonical /<list>/YYYY/MM/<id> URL for an Article in
    `inbox_name`. With cross-posts, the same article can render at
    multiple URLs (one per inbox it's linked to); the caller picks
    based on context (the URL's inbox)."""
    if article.date is not None:
        return f"/{inbox_name}/{article.date.year}/{article.date.month:02d}/{article.id}"
    return f"/{inbox_name}/0000/00/{article.id}"


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


@bp_web.route("/")
def index():
    """Meta-index: list of configured inboxes with per-inbox stats."""
    with SessionLocal() as session:
        inboxes = session.execute(select(Inbox).order_by(Inbox.name)).scalars().all()
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
) -> Response:
    """Render an Atom 1.0 feed from a list of `ArticleSummary`. Uses
    stdlib ElementTree — no extra dep. Emits redacted authors via the
    same `safe_from` rule the HTML side uses, so private email
    addresses don't leak via feed readers either."""
    feed_updated = (
        max((e.date for e in entries if e.date), default=None)
        or datetime.now(timezone.utc)
    )

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
        # RFC 4151 tag URI; the "date" portion is the message's date
        # (stable for the lifetime of the entry, unique per message).
        date_str = a.date.strftime("%Y-%m-%d") if a.date else "1970-01-01"
        SubElement(entry, "id").text = (
            f"tag:{_TAG_URI_AUTHORITY},{date_str}:{inbox_name}/{a.id}"
        )
        SubElement(entry, "title").text = a.subject or "(no subject)"
        if a.date is not None:
            SubElement(entry, "updated").text = a.date.strftime("%Y-%m-%dT%H:%M:%SZ")
        SubElement(
            entry, "link",
            rel="alternate", type="text/html",
            href=msg_base + _msg_url(a, inbox_name),
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


@bp_web.route("/<inbox_name>/feed.atom")
def inbox_feed(inbox_name: str):
    """Atom feed of the most-recent messages in an inbox."""
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        entries = recent_articles(session, inbox, limit=FEED_ENTRY_LIMIT)

    base = request.url_root
    return _atom_response(
        feed_id=f"{base}{inbox.name}/feed.atom",
        feed_title=f"{inbox.name} · {settings.site_name}",
        self_url=f"{base}{inbox.name}/feed.atom",
        alternate_url=f"{base}{inbox.name}/",
        entries=entries,
        inbox_name=inbox.name,
        base_url=base,
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

        # If the message was cross-posted, surface the other inbox
        # links so readers can see the same thread from a different
        # vantage. List excludes the current inbox.
        cross_post_inboxes = [
            n for n, in session.execute(
                select(Inbox.name)
                .join(ArticleList, ArticleList.inbox_id == Inbox.id)
                .where(
                    ArticleList.article_id == article.id,
                    Inbox.id != inbox.id,
                )
                .order_by(Inbox.name)
            )
        ]

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
    )
