"""add backfill_runs table

Revision ID: 11e653c445d0
Revises: 13b7f3111e00
Create Date: 2026-10-11 15:44:02.350558

Persists the cursor of each chunked backfill run so a broker restart or
a killed CLI no longer sends a `--reprocess` walk back to the newest
article (#680). A new, empty table: no existing rows are touched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '11e653c445d0'
down_revision: Union[str, Sequence[str], None] = '13b7f3111e00'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "backfill_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("params_key", sa.String(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=True),
        sa.Column("examined", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # Ids are never reused; see `BackfillRun` in mimir/models.py.
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ux_backfill_runs_unfinished",
        "backfill_runs",
        ["name", "params_key"],
        unique=True,
        sqlite_where=sa.text("finished_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ux_backfill_runs_unfinished", table_name="backfill_runs")
    op.drop_table("backfill_runs")
