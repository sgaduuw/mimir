"""seed robots disallow for the message-id lookup paths

Revision ID: 7484f4bf925c
Revises: 5d7e2a1c9f40
Create Date: 2026-11-03 12:00:00.000000

Appends `/m/` and `/*/m/` to the seeded `*` stanza's disallow list.
Both prefixes hold only the Message-ID lookup (`/m/<id>`, `/m/?id=`,
`/<inbox>/m/<id>`): every URL there is a 301 to a page that has its own
URL, so a crawler gains nothing from them and each typed or guessed ID
would be one more URL to fetch (#984).

A data migration for the reason `e3aa78c72a8d` is one:
`mimir/robots.py`'s `_DEFAULT_STAR_DISALLOW` is read only by
`reset_rules()` and a fresh seed, while `render_robots_txt` reads the
`robots_rules` rows. Same append-only guard as there: add what is
missing, never rewrite the list, so an operator's own entries survive.
A deploy whose operator already added the paths by hand is a no-op.
"""

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7484f4bf925c"
down_revision: Union[str, Sequence[str], None] = "5d7e2a1c9f40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirrors the additions to `_DEFAULT_STAR_DISALLOW` in `mimir/robots.py`
# (fresh deploys get them from the constant; existing rows get them
# here). Keep the two in step.
_ADDED_DISALLOW = ("/m/", "/*/m/")


def upgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT disallow_paths FROM robots_rules WHERE user_agent = '*'")
    ).first()
    # No `*` row: never seeded, or dropped by the operator.
    # `reset_rules` applies the constant when it next runs.
    if row is None:
        return
    # Raw `sa.text` bypasses the JSON type, so the value arrives as the
    # stored string, and both `[]` and the string `'null'` (what the JSON
    # type writes for None once an operator removes the last path) mean
    # "no paths". See e3aa78c72a8d for how that was learned.
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
    # Deliberately a no-op, for the reason given in e3aa78c72a8d: the
    # chain is forward-only, and removing by value would strip a path the
    # operator added themselves.
    pass
