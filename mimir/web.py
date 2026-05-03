from datetime import date as date_cls, datetime, timedelta, timezone
from email.utils import parseaddr
from urllib.parse import quote

from flask import Blueprint, Response, abort, render_template, request
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename, guess_lexer
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound
from sqlalchemy import func, select

from mimir.config import settings
from mimir.dashboard import (
    archive_stats,
    author_recent,
    daily_volume,
    latest_pull_requests,
    latest_stable_releases,
    this_day_in_history,
)
from mimir.extensions import SessionLocal
from mimir.models import Article
from mimir.rendering import URL_OR_MSGID_RE, render_body
from mimir.store import MessageNotFound, read_message
from mimir.threading import active_threads, find_thread_root, get_thread, threads_for_day

bp_web = Blueprint("web", __name__)


@bp_web.app_context_processor
def _inject_list_name() -> dict:
    """Make settings.list_name available to every template (used by nav)."""
    return {"list_name": settings.list_name}


# Cache-Control per endpoint. Lets edge caches (Cloudflare, an nginx in
# front, the browser) absorb scraper traffic on the high-volume endpoints
# without pinning stale content for too long. Attachments are
# byte-stable per (message_id, n), so they're cached aggressively;
# listings are cached briefly so new messages don't take more than ~1
# minute to surface; pagination and 404s/redirects skip caching.
_CACHE_CONTROL_BY_ENDPOINT = {
    "web.index": "public, max-age=60",
    "web.daily_today": "public, max-age=60",
    "web.daily_yesterday": "public, max-age=600",
    "web.message": "public, max-age=60",
    "web.attachment_download": "public, max-age=3600, immutable",
    "web.attachment_preview": "public, max-age=3600, immutable",
}


@bp_web.after_request
def _add_cache_headers(response):
    if response.status_code != 200:
        return response
    rule = _CACHE_CONTROL_BY_ENDPOINT.get(request.endpoint)
    if rule:
        response.headers["Cache-Control"] = rule
    return response


def _msg_url(article: Article) -> str:
    """Build the canonical /<list>/YYYY/MM/<id> URL for an Article.
    Uses the integer primary key, not the Message-ID — Message-IDs leak
    email addresses (some encode the literal local-part) and we want
    URLs to be safe to share, log, and bookmark."""
    if article.date is not None:
        return f"/{settings.list_name}/{article.date.year}/{article.date.month:02d}/{article.id}"
    return f"/{settings.list_name}/0000/00/{article.id}"


@bp_web.app_template_filter("msg_url")
def _msg_url_filter(article: Article) -> str:
    return _msg_url(article)


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


def _fetch_recent(session, offset: int, limit: int):
    """Fetch limit+1 recent articles to detect has_more cheaply."""
    rows = session.execute(
        select(Article)
        .order_by(Article.date.desc().nulls_last())
        .offset(offset)
        .limit(limit + 1)
    ).scalars().all()
    has_more = len(rows) > limit
    return rows[:limit], has_more


@bp_web.route("/")
def index():
    with SessionLocal() as session:
        active = active_threads(session, days=7, limit=10)
        trackers = [
            {"label": label, "messages": author_recent(session, substr, 5)}
            for label, substr in settings.tracked_authors.items()
        ]
        pulls = latest_pull_requests(session, limit=5)
        stable = latest_stable_releases(session, limit=5)
        history = this_day_in_history(session, years_ago=5, limit=3)
        recent, recent_has_more = _fetch_recent(session, 0, RECENT_PAGE_SIZE)
        stats = archive_stats(session)
        spark = daily_volume(session, days=30)
    return render_template(
        "index.html",
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


def _daily_view(list_name: str, day: date_cls, heading: str):
    """Shared renderer for /<list>/today and /<list>/yesterday."""
    if list_name != settings.list_name:
        abort(404)
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    with SessionLocal() as session:
        threads = threads_for_day(session, day)
        total = session.scalar(
            select(func.count(Article.id)).where(
                Article.date >= start.strftime("%Y-%m-%d %H:%M:%S"),
                Article.date < end.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
    return render_template(
        "daily.html",
        day=day,
        heading=heading,
        threads=threads,
        total_messages=total or 0,
    )


@bp_web.route("/<list_name>/today")
def daily_today(list_name: str):
    today = datetime.now(timezone.utc).date()
    return _daily_view(list_name, today, "Today")


@bp_web.route("/<list_name>/yesterday")
def daily_yesterday(list_name: str):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    return _daily_view(list_name, yesterday, "Yesterday")


@bp_web.route("/api/recent")
def api_recent():
    """HTMX load-more endpoint for the Recent messages list. Returns the
    `_recent_items.html` partial: the next page of <li>s plus a fresh
    'Load more' trigger (or nothing, if exhausted)."""
    offset = max(0, request.args.get("offset", default=0, type=int))
    with SessionLocal() as session:
        recent, recent_has_more = _fetch_recent(session, offset, RECENT_PAGE_SIZE)
    return render_template(
        "_recent_items.html",
        recent=recent,
        recent_has_more=recent_has_more,
        recent_next_offset=offset + RECENT_PAGE_SIZE,
    )


def _fetch_article_for_attachment(session, list_name, year, month, article_id, n):
    """Validate URL parts + fetch the n-th attachment via read_message.
    Used by both download and preview routes; aborts 404 on any mismatch."""
    if list_name != settings.list_name:
        abort(404)
    article = session.get(Article, article_id)
    if article is None:
        abort(404)
    if article.date is None or year != article.date.year or month != article.date.month:
        abort(404)
    try:
        parsed = read_message(session, article.message_id)
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
    "/<list_name>/<int:year>/<int:month>/<int:article_id>/attachment/<int:n>"
)
def attachment_download(list_name: str, year: int, month: int, article_id: int, n: int):
    with SessionLocal() as session:
        _, att = _fetch_article_for_attachment(
            session, list_name, year, month, article_id, n
        )
    return Response(
        att.content,
        mimetype=att.content_type or "application/octet-stream",
        headers={"Content-Disposition": _content_disposition(att.filename)},
    )


@bp_web.route(
    "/<list_name>/<int:year>/<int:month>/<int:article_id>/attachment/<int:n>/preview"
)
def attachment_preview(list_name: str, year: int, month: int, article_id: int, n: int):
    with SessionLocal() as session:
        article, att = _fetch_article_for_attachment(
            session, list_name, year, month, article_id, n
        )
    if not _is_previewable(att):
        return render_template(
            "attachment_preview.html",
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
        article=article, att=att, n=n,
        previewable=True,
        highlighted=highlighted,
        lexer_name=lexer.name,
    )


@bp_web.route("/<list_name>/<int:year>/<int:month>/<int:article_id>")
def message(list_name: str, year: int, month: int, article_id: int):
    # Single-list world for now; reject anything else. When multi-list
    # support arrives, this becomes a filter on Article.list instead.
    if list_name != settings.list_name:
        abort(404)

    with SessionLocal() as session:
        article = session.get(Article, article_id)
        if article is None:
            abort(404)

        # The URL date is part of the message's identity; reject mismatches
        # so URLs are a reliable age-gauge.
        if article.date is None or year != article.date.year or month != article.date.month:
            abort(404)

        try:
            parsed = read_message(session, article.message_id)
        except MessageNotFound:
            abort(404)

        # Full thread context (replaces the v1 parent + immediate-replies view).
        root_msgid = find_thread_root(session, article.message_id) or article.message_id
        thread = get_thread(session, root_msgid)

        # If the thread root still has a thread_parent (i.e. our walk-up hit
        # the top of what we have, but the original chain continued through
        # a message not in this archive), surface that as an "off-list"
        # hint in the UI. Common for cross-posted threads from oss-security,
        # linux-arm-kernel, etc.
        parent_off_list: str | None = None
        if thread and thread[0].thread_parent:
            in_db = session.execute(
                select(Article.id).where(Article.message_id == thread[0].thread_parent)
            ).scalar_one_or_none()
            if in_db is None:
                parent_off_list = thread[0].thread_parent

        # When the thread's root has an off-list parent, look for other
        # archived articles with the same normalized subject — likely
        # cross-archive replies to the same conversation that we can offer
        # as "see also" navigation.
        related: list[Article] = []
        if parent_off_list and article.subject_normalized:
            in_thread = {n.message_id for n in thread}
            related = list(
                session.execute(
                    select(Article)
                    .where(
                        Article.subject_normalized == article.subject_normalized,
                        Article.message_id.not_in(in_thread),
                    )
                    .order_by(Article.date.desc().nulls_last())
                    .limit(5)
                ).scalars()
            )

        # Build URLs for thread nodes (avoids per-node template logic).
        # Uses the article's integer id — see _msg_url for the rationale.
        thread_urls = {
            n.message_id: f"/{settings.list_name}/{n.date.year}/{n.date.month:02d}/{n.id}"
            if n.date
            else f"/{settings.list_name}/0000/00/{n.id}"
            for n in thread
        }

        # Resolve in-body <Message-ID> references to canonical URLs. One
        # bulk SELECT instead of N+1 lookups inside the renderer.
        msgid_urls: dict[str, str] = {}
        if parsed.body:
            candidates = {
                m.group("msgid")
                for m in URL_OR_MSGID_RE.finditer(parsed.body)
                if m.group("msgid")
            }
            if candidates:
                referenced = session.execute(
                    select(Article).where(Article.message_id.in_(candidates))
                ).scalars().all()
                msgid_urls = {a.message_id: _msg_url(a) for a in referenced}

    return render_template(
        "message.html",
        parsed=parsed,
        article=article,
        thread=thread,
        thread_urls=thread_urls,
        msgid_urls=msgid_urls,
        parent_off_list=parent_off_list,
        related=related,
    )
