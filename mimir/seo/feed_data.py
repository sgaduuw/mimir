"""Bounded feed listings and their matching canonical link snapshots."""

import hashlib
import json

from sqlalchemy.orm import Session

from mimir import cache
from mimir.config import settings
from mimir.dashboard import (
    FEED_ENTRY_LIMIT,
    FEED_WINDOW_DAYS,
    LISTING_CACHE_TTL_SEC,
    ArticleSummary,
    feed_articles,
)
from mimir.datetime_utils import aware_utc
from mimir.models import Inbox


def feed_data(
    session: Session,
    inbox: Inbox,
    *,
    author: str = "",
    days: int = FEED_WINDOW_DAYS,
    limit: int = FEED_ENTRY_LIMIT,
) -> tuple[list[ArticleSummary], dict[int, str], dict[int, str], int]:
    """Return entries, relative links and canonical inboxes from one snapshot.

    An age-out can reuse a superset of links. A new or changed entry cannot.
    The link row has a hard listing TTL even when a warmer extends its other
    cache rows, so root repairs and removed memberships age out predictably.
    """
    entries = feed_articles(session, inbox, author=author, days=days, limit=limit)
    signature = [(a.id, aware_utc(a.date).isoformat()) for a in entries]
    policy = hashlib.sha256(
        json.dumps(sorted(settings.canonical_demoted_inboxes)).encode()
    ).hexdigest()
    key = (
        f"feed_links:v1:{inbox.name}:{days}:{limit}:"
        f"{settings.thread_view_render_cap}:{policy}:{author}"
    )
    saved = cache.get(key)
    if (
        saved is None
        or saved["expires_at"] <= cache._now()
        or not set(signature).issubset(saved["entries"])
    ):
        # The web package imports SEO while registering routes.
        from mimir.web.urls import _canonical_destinations_for

        expires_at = cache._now() + LISTING_CACHE_TTL_SEC
        destinations = _canonical_destinations_for(session, entries)
        saved = {
            "expires_at": expires_at,
            "entries": signature,
            # Tuples retain integer ids through the existing cache codec.
            # Integer-keyed JSON maps would return string keys on a cache hit.
            "destinations": [
                (aid, destination.url, destination.inbox_name)
                for aid, destination in destinations.items()
            ],
        }
        with cache.warm_window(None):
            cache.set(key, saved, max(0, expires_at - cache._now()))
    links = {aid: url for aid, url, _name in saved["destinations"]}
    names = {aid: name for aid, _url, name in saved["destinations"]}
    return entries, links, names, saved["expires_at"]
