"""Add authoritative subsystem list contacts.

Revision ID: d7e8a9b0c1d2
Revises: c471af7e3d22
"""

import sqlalchemy as sa

from alembic import op

revision = "d7e8a9b0c1d2"
down_revision = "c471af7e3d22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subsystem_lists",
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("subsystem_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["subsystem_id"], ["subsystems.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("address", "subsystem_id"),
    )
    op.create_index(
        "ix_subsystem_lists_subsystem_id", "subsystem_lists", ["subsystem_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_subsystem_lists_subsystem_id", table_name="subsystem_lists")
    op.drop_table("subsystem_lists")
