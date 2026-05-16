"""Attachment endpoints: raw download + Pygments-highlighted preview.

Both routes fetch the parent message via `read_message` (re-parses
the git blob), index into `parsed.attachments[n]`, and either serve
bytes (download, with RFC 6266 Content-Disposition for non-ASCII
filenames) or hand the decoded text to Pygments (preview).
"""
import re
from urllib.parse import quote

from flask import Response, abort, render_template
from pygments import highlight
from pygments.formatters import HtmlFormatter
from sqlalchemy import select
from sqlalchemy.orm import Session

from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, Inbox
from mimir.store import MessageNotFound, read_message
from mimir.web._blueprint import bp_web
from mimir.web.filters import _is_previewable, _lexer_for
from mimir.web.urls import _abort_404_if_url_date_mismatches, _get_inbox_or_404


_HEADER_CTL_RE = re.compile(r"[\x00-\x1f\x7f]")


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
    _abort_404_if_url_date_mismatches(article, year, month)
    try:
        parsed = read_message(session, inbox, article.message_id)
    except MessageNotFound:
        abort(404)
    if n < 0 or n >= len(parsed.attachments):
        abort(404)
    return article, parsed.attachments[n]


def _content_disposition(filename: str | None) -> str:
    """Build a Content-Disposition header that handles non-ASCII filenames
    via RFC 6266 (filename* parameter).

    Strips control bytes (CR, LF, NUL, tab, DEL) from the ASCII
    `filename="…"` form: a maliciously-crafted attachment filename
    carrying CR/LF would otherwise inject extra HTTP response
    headers (RFC 7230 header-line splitting). Defense in depth on
    top of whatever the WSGI layer rejects. The percent-encoded
    `filename*` form is unaffected, `quote()` already escapes
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
