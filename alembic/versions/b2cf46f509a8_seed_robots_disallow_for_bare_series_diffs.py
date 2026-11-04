"""seed robots disallow for bare series diffs

Revision ID: b2cf46f509a8
Revises: 7484f4bf925c
Create Date: 2026-11-04 07:00:00.000000

Append the end-anchored bare-diff rule without changing operator entries.
Full comparison URLs carry a query string and must remain crawlable.
This revision runs on fresh and existing upgrade chains; reset reads
mimir.robots._DEFAULT_STAR_DISALLOW independently.
"""

import json

from alembic import op
import sqlalchemy as sa

revision = "b2cf46f509a8"
down_revision = "7484f4bf925c"
branch_labels = None
depends_on = None

_PATTERN = "/*/series/*/diff$"


def upgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT disallow_paths FROM robots_rules WHERE user_agent = '*'")
    ).first()
    if row is None:
        return
    # Raw SQL sees both SQL NULL and JSON null; both mean no paths.
    paths = (json.loads(row[0]) if row[0] else None) or []
    if _PATTERN not in paths:
        conn.execute(
            sa.text(
                "UPDATE robots_rules SET disallow_paths = :paths WHERE user_agent = '*'"
            ).bindparams(paths=json.dumps([*paths, _PATTERN]))
        )


def downgrade() -> None:
    # Removing by value could erase a rule the operator added themselves.
    pass
