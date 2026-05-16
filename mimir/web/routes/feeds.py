"""Atom feeds: per-inbox and per-author.

Both share the same `atom_response` builder from `mimir.seo`; the
author-feed clamping reuses the search-route bounds via the
`mimir.web.routes.search` constants.
"""
from urllib.parse import quote

from flask import abort

from mimir.config import settings
from mimir.dashboard import author_recent, recent_articles
from mimir.extensions import SessionLocal
from mimir.seo import atom_response
from mimir.web._blueprint import bp_web
from mimir.web.routes.search import SEARCH_QUERY_MAX_LEN, SEARCH_QUERY_MIN_LEN
from mimir.web.urls import _canonical_inbox_names_for, _get_inbox_or_404, _site_base


FEED_ENTRY_LIMIT = 50


@bp_web.route("/<inbox_name>/feed.atom")
def inbox_feed(inbox_name: str):
    """Atom feed of the most-recent messages in an inbox."""
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        entries = recent_articles(session, inbox, limit=FEED_ENTRY_LIMIT)
        canonical_map = _canonical_inbox_names_for(session, [e.id for e in entries])

    base = _site_base() + "/"
    return atom_response(
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
    substring of From, same shape the dashboard tracker uses, scoped
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
    return atom_response(
        feed_id=f"{base}{inbox.name}/author/{sub_quoted}/feed.atom",
        feed_title=f"{sub} on {inbox.name} | {settings.site_name}",
        self_url=f"{base}{inbox.name}/author/{sub_quoted}/feed.atom",
        alternate_url=f"{base}{inbox.name}/",
        entries=entries,
        inbox_name=inbox.name,
        base_url=base,
        canonical_inbox_by_article=canonical_map,
    )
