"""Seven-day authored-message ranking for current, addressable maintainers.

Measured 2026-11-15 UTC on a read-only production snapshot: 47,280 messages
and 13,228 review rows in seven days, 0.81 s forced computation on Python
3.15.2 free-threaded / SQLite 3.53.1, excluding cache publication.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

from sqlalchemy import DateTime, bindparam, select, text
from sqlalchemy.orm import Session

from mimir import cache, mailmap
from mimir.maintainer_directory import (
    MAINTAINER_DIRECTORY_CACHE_TTL_SEC,
    all_maintainers,
    is_addressable_maintainer_address,
)
from mimir.models import Mailmap
from mimir.trailers import REVIEW_TRAILER_ROLES


class _RulesChanged(Exception):
    """Abort cache publication when identity inputs changed during computation."""


def _now() -> datetime:
    return datetime.now(UTC)


def _fold(address: str, aliases: dict[str, str], names: dict[str, str]) -> str:
    """Fold a retired address only into a current M: address. A maintainer
    still listed under an address `.mailmap` retires keeps it: folding it to
    a target that is not M: would drop them from the ranking, and their
    profile is served at the listed address (`identity_addresses` semantics)."""
    mapped = aliases.get(address)
    return mapped if mapped in names else address


def most_active_maintainers(
    session: Session,
    *,
    days: int = 7,
    limit: int = 12,
    force: bool = False,
    compute_on_miss: bool = True,
) -> list[dict]:
    """Rank current M entries by unique messages authored, with review counts.

    The web only reads the slow warmer's cache. Cache the full ranking so
    changing a display limit cannot cause a cold scan. Identity generations
    include forced reloads, even when the source files are unchanged.
    """
    version = mailmap.rules_version(session)
    key = f"most_active_maintainers:v1:{days}:{version}"

    def compute():
        until = _now()
        bounds = {"since": until - timedelta(days=days), "until": until}
        # The directory's fixed cache key can survive a rules reload; read its
        # source when recomputing, never on the cache-only web path.
        names = {
            address: name or address
            for address, name in all_maintainers(session, force=True)
            if is_addressable_maintainer_address(address)
        }
        aliases = {
            old: current
            for old, current in session.execute(
                select(Mailmap.old_address, Mailmap.address)
            )
        }
        messages = Counter()
        # No article_lists join: a cross-post is one article. Stream raw headers
        # instead of retaining every message or loading ORM entities.
        authors = session.execute(
            text("""
                SELECT author FROM articles INDEXED BY ix_articles_date
                WHERE date >= :since AND date < :until
            """).bindparams(
                bindparam("since", type_=DateTime), bindparam("until", type_=DateTime)
            ),
            bounds,
            execution_options={"yield_per": 1000},
        )
        try:
            for (author,) in authors:
                address = parseaddr(author or "")[1].strip().lower()
                local, _, domain = address.rpartition("@")
                # parseaddr accepts bare words. A mailmap row must not turn
                # a missing mailbox into an authored message; retired RFC
                # addresses may still be wider than our profile URL grammar.
                if not local or not domain:
                    continue
                address = _fold(address, aliases, names)
                if address in names:
                    messages[address] += 1
        finally:
            authors.close()
        reviews = Counter()
        # CROSS JOIN keeps the bounded date seek outside trailer lookup even
        # with production statistics favouring the role/address index.
        rows = session.execute(
            text("""
                SELECT t.address_normalized, count(*)
                FROM articles AS a INDEXED BY ix_articles_date
                CROSS JOIN article_trailers AS t ON t.article_id = a.id
                WHERE a.date >= :since AND a.date < :until
                  AND t.role IN :roles
                GROUP BY t.address_normalized
            """).bindparams(
                bindparam("since", type_=DateTime),
                bindparam("until", type_=DateTime),
                bindparam("roles", expanding=True),
            ),
            {**bounds, "roles": REVIEW_TRAILER_ROLES},
        )
        try:
            for address, count in rows:
                address = _fold(address.strip().lower(), aliases, names)
                if address in messages:
                    reviews[address] += count
        finally:
            rows.close()
        # SELECT again after closing both cursors: broker read sessions do not
        # pin a transaction snapshot. A replacement between input reads must
        # not produce a mixed-generation cache entry.
        if mailmap.rules_version(session) != version:
            raise _RulesChanged
        return [
            {
                "address": address,
                "name": names[address],
                "message_count": messages[address],
                "review_count": reviews[address],
            }
            for address in sorted(messages, key=lambda a: (-messages[a], a))
        ]

    if not compute_on_miss:
        rows = cache.get(key) or []
    else:
        try:
            rows = cache.get_or_compute(
                session, key, MAINTAINER_DIRECTORY_CACHE_TTL_SEC, compute, force=force
            )
        except _RulesChanged:
            return []
    # The route rechecks addressability; compute() admitted only addressable names.
    return rows[:limit]
