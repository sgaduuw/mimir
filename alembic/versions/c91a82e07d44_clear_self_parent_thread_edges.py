"""Clear indexed self-parent thread edges.

Revision ID: c91a82e07d44
Revises: b7e41d0c9a26
Create Date: 2026-11-22

A message cannot reply to itself. Ingest now ignores that header edge,
while this broker-startup data repair removes historical occurrences.
Existing root assignment already treats self-parents as roots, so
article-list memberships and materialised root IDs remain unchanged.

Downgrade keeps the repaired data. The prior invalid edge cannot be
reconstructed from NULL without reopening the source message.
"""

from alembic import op

revision = "c91a82e07d44"
down_revision = "b7e41d0c9a26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE articles SET thread_parent = NULL WHERE thread_parent = message_id"
    )


def downgrade() -> None:
    pass
