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


def upgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT disallow_paths FROM robots_rules WHERE user_agent = '*'")
    ).first()
    # No `*` row means the table was never seeded (a deploy that hasn't
    # bootstrapped, or one where the operator dropped it). `reset_rules`
    # applies the constant when it next runs; nothing to backfill.
    if row is None:
        return
    # Raw `sa.text` bypasses the JSON type, so the value arrives as the
    # stored string. Two different stored shapes both mean "no disallow
    # paths": `[]`, and the string `'null'` that SQLAlchemy's JSON type
    # writes for Python None, which is what `update_rule` stores
    # (`paths or None`) once an operator removes the last path. Collapse
    # both to [] so the append still happens; treating the decoded None
    # as "no row" would silently no-op the backfill on exactly the
    # deploy that has customised its robots.txt.
    paths = (json.loads(row[0]) if row[0] else None) or []
    missing = [p for p in _ADDED_DISALLOW if p not in paths]
    if not missing:
        return
    conn.execute(
        sa.text(
            "UPDATE robots_rules SET disallow_paths = :paths WHERE user_agent = '*'"
        ).bindparams(paths=json.dumps(paths + missing))
    )


def downgrade() -> None:
    # Deliberately a no-op. This chain is forward-only (c8e2a47f1d20
    # raises NotImplementedError, 2892bee6ba19 is a bare pass) and
    # mimir's release flow runs no migration smoke, so a data reversal
    # here would be dead code. It would also be actively wrong:
    # removing by value cannot distinguish a path this revision added
    # from one the operator added themselves, so it would strip an
    # operator's own `/api/` entry, breaking the very guarantee
    # upgrade()'s append-only guard exists to provide. Two extra
    # Disallow lines on an older mimir are harmless.
    pass
