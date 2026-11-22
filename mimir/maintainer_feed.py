"""Exact From identities across the configured inboxes, bounded by date."""

from collections.abc import Sequence
from datetime import datetime
from email.utils import parseaddr

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.orm import Session

from mimir.dashboard import ArticleSummary, like_escape
from mimir.datetime_utils import coerce_dt


def maintainer_feed_articles(
    session: Session, addresses: Sequence[str], floor: datetime, limit: int
) -> list[ArticleSummary]:
    """Return newest exact parsed senders, applying the cap after parsing."""
    identities = {address.lower() for address in addresses if address}
    if not identities or limit <= 0:
        return []
    binds = {"floor": floor}
    author_where = "1"
    if all(address.isascii() for address in identities):
        terms = []
        for index, address in enumerate(sorted(identities)):
            name = f"author_{index}"
            terms.append(rf"replace(a.author, ' ', '') LIKE :{name} ESCAPE '\'")
            binds[name] = f"%{like_escape(address.replace(' ', ''))}%"
        # parseaddr accepts comments, quoting and internal whitespace.
        # Admit decorated headers so the SQL prefilter cannot lose split addresses.
        terms.append("a.author GLOB :decorated")
        # Python lower() can map non-ASCII characters to an ASCII identity.
        terms.append("a.author GLOB '*[^ -~]*'")
        binds["decorated"] = '*[\t\r\n\v\f()"\\]*'
        author_where = " OR ".join(terms)
    stmt = text(
        "SELECT a.id, a.subject, a.author, a.date "
        "FROM articles AS a INDEXED BY ix_articles_date "
        "WHERE a.date >= :floor "
        f"AND ({author_where}) "
        "AND EXISTS (SELECT 1 FROM article_lists AS al "
        "JOIN inboxes AS i ON i.id = al.inbox_id "
        "WHERE al.article_id = a.id) "
        "ORDER BY a.date DESC, a.id DESC"
    ).bindparams(bindparam("floor", type_=DateTime))
    entries = []
    rows = session.execute(stmt, binds).yield_per(200)
    try:
        for row in rows:
            if parseaddr(row.author or "")[1].lower() not in identities:
                continue
            entries.append(
                ArticleSummary(
                    id=row.id,
                    subject=row.subject,
                    author=row.author,
                    date=coerce_dt(row.date),
                )
            )
            if len(entries) == limit:
                break
    finally:
        rows.close()
    return entries
