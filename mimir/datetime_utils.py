"""Datetime normalization for the naive-vs-aware boundary cases that
recur across ingest, patch-state, and web/render code.

Two sources of naive datetimes in mimir:

1. `email.utils.parsedate_to_datetime` returns naive when the RFC 5322
   `Date:` header carries `-0000` (the spec's "no time-zone available"
   sentinel). Mixing naive into a max() with a tz-aware date raises
   TypeError; an ingest batch hits this the moment one such message
   arrives, rolling back the whole batch. See CONTEXT.md "tz-aware UTC
   normalization for `Date:` headers" for the production incident that
   forced the helper.

2. The threading recursive-CTE in `mimir/threading.py` round-trips the
   `articles.date` column through SQLite's TEXT storage and parses it
   back via `fromisoformat`, which doesn't recover the original tz
   suffix for naive ISO strings. Downstream `datetime.now(tz) - dt`
   blows up unless callers re-aware the value.

The convention across the codebase: treat naive as UTC, consistent
with RFC 5322's `-0000` semantics and with how lore.kernel.org's
commit timestamps already arrive.

A standalone module (rather than a helper inside `parser.py` or
`extensions.py`) keeps the concern self-describing: anywhere a naive
datetime might cross a comparison boundary, import `aware_utc` from
here. One concern per file, per CLAUDE.md.
"""

from datetime import datetime, timezone


def aware_utc(dt: datetime) -> datetime:
    """Return `dt` as a tz-aware UTC datetime.

    No-op if `dt` already carries a timezone; replaces a missing
    tzinfo with `timezone.utc` otherwise. Callers handle `None`
    themselves (the helper expects a real datetime; passing `None`
    would mask a missing-data bug at the call site).
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
