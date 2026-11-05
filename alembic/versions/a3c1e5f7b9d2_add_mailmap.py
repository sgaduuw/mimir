"""Add the kernel .mailmap address pairs.

Revision ID: a3c1e5f7b9d2
Revises: d7e8a9b0c1d2
"""

import sqlalchemy as sa

from alembic import op

revision = "a3c1e5f7b9d2"
down_revision = "d7e8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mailmap",
        sa.Column("old_address", sa.String(), nullable=False),
        sa.Column("address", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("old_address"),
    )


def downgrade() -> None:
    op.drop_table("mailmap")
