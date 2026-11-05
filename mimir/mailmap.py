"""Reads of the kernel `.mailmap` pairs the rules loader stores (#986).

`.mailmap` is the only source of identity: a retired address maps to
one canonical address, one hop, ambiguous old addresses have no row.
The write side is `mimir.mainline`; this module only reads.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from mimir.models import Mailmap, MainlineState


def rules_version(session: Session) -> str:
    """The id of the MAINTAINERS blob (plus `:<mailmap blob id>`) at the
    last load. It moves in the same transaction as the pairs, so a cache
    key carrying it cannot outlive the mapping it was computed from."""
    return (
        session.scalar(
            select(MainlineState.last_commit_sha).where(
                MainlineState.tree_name == "linus"
            )
        )
        or ""
    )


def identity_addresses(session: Session, address: str) -> list[str]:
    """`address` plus every retired address `.mailmap` maps to it, for
    matching trailers that belong to one person. An address that is
    itself retired has no retired addresses of its own (one hop), so it
    keeps matching only its own trailers."""
    olds = session.scalars(
        select(Mailmap.old_address).where(Mailmap.address == address)
    )
    return [address, *sorted(olds)]


def canonical_address(session: Session, address: str) -> str | None:
    """The address `.mailmap` maps `address` to, or None. One PK lookup."""
    return session.scalar(select(Mailmap.address).where(Mailmap.old_address == address))
