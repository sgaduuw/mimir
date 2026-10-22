"""add articles.patch_series_head_id

Revision ID: 4c1d816a0b2e
Revises: 9b2878b4f911
Create Date: 2026-10-21 18:00:00.000000

A nullable integer that splits one patch_series_key into the separate
series a sender posted under one cover title (#816). NULL everywhere
until the chain backfill writes it, and NULL reads as the key's only
series, so this deploys as a no-op. ADD COLUMN of a nullable column
without a default is a schema-only change in SQLite (no table rewrite),
and there is no index and no backfill here, so the broker's startup
migration stays O(1) on the full archive.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "4c1d816a0b2e"
down_revision: Union[str, Sequence[str], None] = "9b2878b4f911"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "articles", sa.Column("patch_series_head_id", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("articles", "patch_series_head_id")
