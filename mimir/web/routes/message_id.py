"""Message-ID lookup redirects.

People share Message-IDs in commit trailers, bug reports, lore links,
IRC; the canonical /<inbox>/<YYYY>/<MM>/<article-id> URL requires
knowing the date. Both routes 301-redirect to the canonical URL:
- `/m/<id>` → the article's canonical-inbox URL.
- `/<inbox>/m/<id>` → the inbox-scoped URL (preserves the user's
  explicit choice; the destination page's `<link rel="canonical">`
  still points at the canonical for crawlers).
"""
from flask import abort, redirect
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, Inbox
from mimir.web._blueprint import bp_web
from mimir.web.urls import _canonical_inbox_name, _get_inbox_or_404, _msg_url


@bp_web.route("/m/<path:message_id>")
def message_id_lookup(message_id: str):
    """Resolve a bare Message-ID to its canonical URL.

    For cross-posts, redirects directly to the canonical inbox's URL
    (article.canonical_inbox_id; alphabetically-first linked inbox
    when canonical is unset). The message page's "Also in:" line
    surfaces the other inboxes from there. 301 because the target
    is stable for the article's lifetime, transfers link equity
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
