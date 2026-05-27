"""add mainline_state.last_walked_at

Revision ID: 01342fe0a018
Revises: 82e825291162
Create Date: 2026-05-26 13:34:35.315332

Adds a per-tree last-walked timestamp to mainline_state so
update_mainline() can gate per-tree work on walk_every_seconds.
NULL on existing rows means "due immediately" on next tick;
no data backfill required.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '01342fe0a018'
down_revision: Union[str, Sequence[str], None] = '82e825291162'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mainline_state",
        sa.Column("last_walked_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("mainline_state") as batch:
        batch.drop_column("last_walked_at")
