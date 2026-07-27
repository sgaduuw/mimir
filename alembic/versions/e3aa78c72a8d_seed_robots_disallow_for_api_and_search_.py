"""seed robots disallow for api and search surfaces

Revision ID: e3aa78c72a8d
Revises: 01342fe0a018
Create Date: 2026-07-27 13:49:20.805353

Appends `/api/` and `/*/search` to the seeded `*` stanza's disallow
list, so crawl budget goes to archive content instead of two surfaces
that can never be a useful search result:

- `/api/<inbox>/recent?offset=N` returns an HTML *partial* for the
  htmx load-more control, not a page. Every offset is a distinct URL
  over the same listing.
- `/<inbox>/search?q=...` is an internal search-results page. Google's
  own guidance is to keep these out of the crawl (search results
  inside search results); each `?q=` is a distinct URL rendering a
  thin, duplicate slice of content already indexed at its own URL.

Robots-disallow rather than `noindex` for the search surface,
deliberately. The two don't compose: a disallowed URL is never
fetched, so a `noindex` on it is unreachable, and mixing the signals
is a documented way to confuse consolidation. Disallow is also the
stronger lever for the actual goal here, which is crawl budget rather
than index hygiene. `?q=` URLs already canonicalise to the bare
`/<inbox>/search` (the context processor's canonical drops the query
string), so the index side was covered before this change.

This is a data migration because `mimir/robots.py`'s
`_DEFAULT_STAR_DISALLOW` is only consulted by `reset_rules()` and a
fresh seed, while `render_robots_txt` reads `robots_rules` rows.
Changing the constant alone would leave every existing deploy serving
the old robots.txt forever, i.e. shipping the change silently inert.
Same shape and the same operator-edit guard as the content_signals
backfill in 82e825291162: append only what's missing, never rewrite
the list, so an operator who curated their own disallow set keeps it.
"""

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e3aa78c72a8d"
down_revision: Union[str, Sequence[str], None] = "01342fe0a018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirrors the additions to `_DEFAULT_STAR_DISALLOW` in `mimir/robots.py`
# (fresh deploys get them from the constant; existing rows get them
# here). Keep the two in step.
_ADDED_DISALLOW = ("/api/", "/*/search")


def _star_disallow_paths(conn) -> list[str] | None:
    """Current disallow list on the `*` row, or None when there is no
    such row. Raw `sa.text` bypasses the JSON type, so the value
    arrives as the stored string rather than a decoded list."""
    row = conn.execute(
        sa.text("SELECT disallow_paths FROM robots_rules WHERE user_agent = '*'")
    ).first()
    if row is None:
        return None
    return json.loads(row[0]) if row[0] else []


def _write_star_disallow_paths(conn, paths: list[str]) -> None:
    conn.execute(
        sa.text(
            "UPDATE robots_rules SET disallow_paths = :paths WHERE user_agent = '*'"
        ).bindparams(paths=json.dumps(paths))
    )


def upgrade() -> None:
    conn = op.get_bind()
    paths = _star_disallow_paths(conn)
    # No `*` row means the table was never seeded (a deploy that hasn't
    # bootstrapped, or one where the operator dropped it). `reset_rules`
    # applies the constant when it next runs; nothing to backfill.
    if paths is None:
        return
    missing = [p for p in _ADDED_DISALLOW if p not in paths]
    if not missing:
        return
    _write_star_disallow_paths(conn, paths + missing)


def downgrade() -> None:
    conn = op.get_bind()
    paths = _star_disallow_paths(conn)
    if paths is None:
        return
    kept = [p for p in paths if p not in _ADDED_DISALLOW]
    if kept != paths:
        _write_star_disallow_paths(conn, kept)
