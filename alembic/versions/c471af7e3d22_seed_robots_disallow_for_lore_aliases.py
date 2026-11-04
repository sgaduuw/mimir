"""reserve lore alias prefixes and append their robots disallows

Revision ID: c471af7e3d22
Revises: b2cf46f509a8
Create Date: 2026-11-04 08:00:00.000000

The old deployed version must inventory and resolve r/all inbox conflicts
before upgrading: the new wire predicate also reserves rename sources.
This guard fails before any robots change if a persisted conflict remains.
Fresh and existing upgrade chains append only missing disallow entries;
reset reads mimir.robots._DEFAULT_STAR_DISALLOW independently.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "c471af7e3d22"
down_revision = "b2cf46f509a8"
branch_labels = None
depends_on = None

_ADDED_DISALLOW = ("/r/", "/all/")


def upgrade() -> None:
    conn = op.get_bind()
    conflicts = conn.execute(
        sa.text("SELECT name FROM inboxes WHERE name IN ('r', 'all') ORDER BY name")
    ).scalars().all()
    if conflicts:
        raise RuntimeError(
            "Reserved lore alias inbox names exist: "
            + ", ".join(conflicts)
            + ". Resolve these names using the old deployed version before upgrading."
        )
    row = conn.execute(
        sa.text("SELECT disallow_paths FROM robots_rules WHERE user_agent = '*'")
    ).first()
    if row is None:
        return
    paths = (json.loads(row[0]) if row[0] else None) or []
    missing = [path for path in _ADDED_DISALLOW if path not in paths]
    if missing:
        conn.execute(
            sa.text(
                "UPDATE robots_rules SET disallow_paths = :paths WHERE user_agent = '*'"
            ).bindparams(paths=json.dumps(paths + missing))
        )


def downgrade() -> None:
    # Removing by value could erase a rule the operator added themselves.
    pass
