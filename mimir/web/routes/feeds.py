"""Atom feeds: per-inbox, per-author and cross-inbox per-maintainer.

All share the same `atom_response` builder from `mimir.seo`; the
author-feed clamping reuses the search-route bounds via the
`mimir.web.routes.search` constants.
"""

from urllib.parse import quote

from flask import abort, redirect

from mimir._inbox_name import inbox_path
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.maintainer_directory import maintainer_path, resolve_maintainer_profile
from mimir.seo import atom_response
from mimir.seo.feed_data import feed_data, maintainer_feed_data
from mimir.web._blueprint import bp_web
from mimir.web.filters import _listing_name_filter
from mimir.web.routes.search import SEARCH_QUERY_MAX_LEN, SEARCH_QUERY_MIN_LEN
from mimir.web.urls import (
    _get_inbox_or_404,
    _site_base,
)


@bp_web.route("/<inbox_name>/feed.atom")
def inbox_feed(inbox_name: str):
    """Atom feed of the most-recent messages in an inbox."""
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        entries, entry_urls, canonical_names, expires_at = feed_data(session, inbox)

    base = _site_base() + "/"
    return atom_response(
        feed_id=f"{base}{inbox.name}/feed.atom",
        feed_title=f"{inbox.name} | {settings.site_name}",
        self_url=f"{base}{inbox.name}/feed.atom",
        alternate_url=base.rstrip("/") + inbox_path(inbox.name),
        entries=entries,
        inbox_name=inbox.name,
        base_url=base,
        canonical_inbox_by_article=canonical_names,
        entry_urls=entry_urls,
        link_expires_at=expires_at,
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
        entries, entry_urls, canonical_names, expires_at = feed_data(
            session, inbox, author=sub
        )

    base = _site_base() + "/"
    sub_quoted = quote(sub, safe="")
    return atom_response(
        feed_id=f"{base}{inbox.name}/author/{sub_quoted}/feed.atom",
        feed_title=f"{sub} on {inbox.name} | {settings.site_name}",
        self_url=f"{base}{inbox.name}/author/{sub_quoted}/feed.atom",
        alternate_url=base.rstrip("/") + inbox_path(inbox.name),
        entries=entries,
        inbox_name=inbox.name,
        base_url=base,
        canonical_inbox_by_article=canonical_names,
        entry_urls=entry_urls,
        link_expires_at=expires_at,
    )


# A string converter ties the directory's letter converter in Werkzeug.
# The profile's path converter plus this suffix gives an unambiguous match.
@bp_web.route("/maintainers/<path:address>/feed.atom")
def maintainer_feed(address: str):
    """Recent exact From identities for a current maintainer across inboxes."""
    with SessionLocal() as session:
        profile = resolve_maintainer_profile(session, address)
        if profile is None:
            abort(404)
        path = maintainer_path(profile.address)
        if profile.address != address.lower():
            return redirect(path + "/feed.atom", code=301)
        entries, entry_urls, canonical_names, expires_at = maintainer_feed_data(
            session, profile.address
        )
    name = _listing_name_filter(profile.name)
    if name == "unknown sender":
        name = profile.address
    base = _site_base()
    feed_url = base + path + "/feed.atom"
    return atom_response(
        feed_id=feed_url,
        feed_title=f"{name} | Maintainers | {settings.site_name}",
        self_url=feed_url,
        alternate_url=base + path,
        entries=entries,
        inbox_name="",
        base_url=base,
        canonical_inbox_by_article=canonical_names,
        entry_urls=entry_urls,
        link_expires_at=expires_at,
    )
