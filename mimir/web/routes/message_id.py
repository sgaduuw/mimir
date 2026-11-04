"""Message-ID lookup redirects.

People share Message-IDs in commit trailers, bug reports, lore links,
IRC; the canonical /<inbox>/<YYYY>/<MM>/<article-id> URL requires
knowing the date. These routes 301-redirect to a message URL:
- `/m/<id>` → the article's canonical-inbox URL.
- `/<inbox>/m/<id>` → the inbox-scoped URL (preserves the user's
  explicit choice; the destination page's `<link rel="canonical">`
  still points at the canonical for crawlers).
- `/m/?id=<text>` → the same as `/m/<id>`, for a GET form (#984).
- `/r/<id>` and `/all/<id>/` → the same global lookup, accepting pasted
  lore shapes without emitting another URL spelling (#1053).

All lookup forms read the ID through `normalise_message_id`, so what the form
accepts and what the routes accept cannot drift apart.
"""

import re
from urllib.parse import unquote, urlsplit

from flask import abort, redirect, request
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, Inbox
from mimir.web._blueprint import bp_web
from mimir.web.urls import _canonical_inbox_name, _get_inbox_or_404, _msg_url

# RFC 5322 caps a header line at 998 characters, so no real ID is longer.
_MAX_ID_LEN = 998
# Whitespace and control bytes: none belongs in an ID, and one inside a
# Location header or a log line is trouble.
_BAD_ID_CHARS = re.compile(r"[\x00-\x20\x7f]")


def normalise_message_id(raw: str | None) -> str | None:
    """The Message-ID in what a person typed or pasted, or None.

    Accepts the bare ID, `<id>`, `mid:<id>`, and any http(s) URL or
    absolute path with a segment holding an `@`, the first one:
    lore's `https://lore.kernel.org/<list>/<id>/` (and its `/T/`, `/raw`
    and `/t.mbox.gz` forms), its `/all/<id>/` and `/r/<id>`, and a bare
    `/<list>/<id>/` pasted onto this host. Only the
    ID is taken from a URL, never the host, so the text cannot steer a
    redirect. A bare ID may contain a slash (real ones do), so only a
    leading `/` or a scheme makes the input a path.
    """
    text = (raw or "").strip()
    if not text or len(text) > _MAX_ID_LEN:
        return None
    if text.startswith("/") or "://" in text:
        try:
            parts = urlsplit(text)
        except ValueError:
            return None
        if parts.scheme not in ("", "http", "https"):
            return None
        segments = [unquote(s) for s in parts.path.split("/") if s]
        # The FIRST segment holding an `@`, as `msgid_from_url` reads it:
        # lore appends `T/`, `raw` or `t.mbox.gz` after the ID. Not reused
        # directly because it only knows lore and patch.msgid.link and wants
        # a scheme, while a person also pastes a bare path or our own host.
        candidate = next((seg for seg in segments if "@" in seg), "")
        if not candidate:
            return None
    else:
        candidate = text[4:] if text[:4].lower() == "mid:" else text
    if candidate.startswith("<") and candidate.endswith(">"):
        candidate = candidate[1:-1]
    if not candidate or _BAD_ID_CHARS.search(candidate):
        return None
    return candidate


@bp_web.route("/m/")
def message_id_lookup_form():
    """The GET form's target: `?id=` carries what was typed. A miss is a
    plain 404 (the error page offers the form again)."""
    return _lookup_global(request.args.get("id"))


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
    return _lookup_global(message_id)


@bp_web.route("/r/<path:message_id>")
def message_id_lookup_alias(message_id: str):
    """Accept a pasted lore r shape; the site never emits this alias."""
    return _lookup_global(message_id)


@bp_web.route("/all/<path:message_id>", merge_slashes=False)
def message_id_lookup_all_alias(message_id: str):
    """Consume one lore delimiter without normalizing the ID's slashes.

    A terminal slash in an ID needs two slashes here: its own and the
    alias delimiter. A trailing-slash route would normalize those into
    another ID before this handler runs. This remains an input-only alias.
    """
    return _lookup_global(message_id.removesuffix("/"))


def _lookup_global(raw: str | None):
    message_id = normalise_message_id(raw)
    if message_id is None:
        abort(404)
    with SessionLocal() as session:
        article = session.execute(
            select(Article).where(Article.message_id == message_id)
        ).scalar_one_or_none()
        if article is None:
            abort(404)
        links = list(
            session.execute(
                select(Inbox.id, Inbox.name)
                .join(ArticleList, ArticleList.inbox_id == Inbox.id)
                .where(ArticleList.article_id == article.id)
            ).all()
        )
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
    message_id = normalise_message_id(message_id)
    if message_id is None:
        abort(404)
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
