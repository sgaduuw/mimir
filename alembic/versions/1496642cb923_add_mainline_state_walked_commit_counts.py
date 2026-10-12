"""add mainline_state walked commit counts

Revision ID: 1496642cb923
Revises: 11e653c445d0
Create Date: 2026-10-12 17:43:16.126202

Two nullable counters on mainline_state for a rebasing tree's last
rebuild: non-merge commits walked and how many link a message. They
let /merge-window say how much of linux-next it can show (#703).
NULL on existing rows until the next rebuild writes them; no data
backfill required.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "1496642cb923"
down_revision: Union[str, Sequence[str], None] = "11e653c445d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mainline_state", sa.Column("walked_commits", sa.Integer(), nullable=True)
    )
    op.add_column(
        "mainline_state", sa.Column("walked_linked", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("mainline_state") as batch:
        batch.drop_column("walked_linked")
        batch.drop_column("walked_commits")
