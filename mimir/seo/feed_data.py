"""Bounded feed listings and their matching canonical link snapshots."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

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
from mimir.mailmap import identity_addresses, rules_version
from mimir.maintainer_feed import maintainer_feed_articles
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
    return _feed_link_snapshot(
        session, entries, f"feed_links:v1:{inbox.name}:{days}:{limit}", author
    )


def maintainer_feed_data(
    session: Session,
    address: str,
    *,
    days: int = FEED_WINDOW_DAYS,
    limit: int = FEED_ENTRY_LIMIT,
    compute_on_miss: bool = True,
) -> tuple[list[ArticleSummary], dict[int, str], dict[int, str], int] | None:
    """Cache exact sender identities and destinations under the loaded rules."""
    address = address.lower()
    version = rules_version(session)
    floor = datetime.now(UTC) - timedelta(days=days)

    def compute() -> list[ArticleSummary]:
        return maintainer_feed_articles(
            session, identity_addresses(session, address), floor, limit
        )

    key = f"maintainer_feed_articles:v1:{days}:{limit}:{version}:{address}"
    rows = (
        cache.get_or_compute(session, key, LISTING_CACHE_TTL_SEC, compute)
        if compute_on_miss
        else cache.get(key)
    )
    if rows is None:
        return None
    entries = [
        article
        for article in rows
        if article.date is not None and aware_utc(article.date) >= floor
    ]
    return _feed_link_snapshot(
        session,
        entries,
        f"maintainer_feed_links:v1:{days}:{limit}:{version}",
        address,
        compute_on_miss=compute_on_miss,
    )


def _feed_link_snapshot(
    session: Session,
    entries: list[ArticleSummary],
    key_prefix: str,
    identity: str,
    *,
    compute_on_miss: bool = True,
) -> tuple[list[ArticleSummary], dict[int, str], dict[int, str], int] | None:
    """Keep link staleness bounded independently of generic cache warming."""
    signature = [(a.id, aware_utc(a.date).isoformat()) for a in entries]
    policy = hashlib.sha256(
        json.dumps(sorted(settings.canonical_demoted_inboxes)).encode()
    ).hexdigest()
    key = f"{key_prefix}:{settings.thread_view_render_cap}:{policy}:{identity}"
    saved = cache.get(key)
    if (
        saved is None
        or saved["expires_at"] <= cache._now()
        or not set(signature).issubset(saved["entries"])
    ):
        if not compute_on_miss:
            return None
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
