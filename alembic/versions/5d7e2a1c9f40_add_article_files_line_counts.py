"""add article_files line counts

Revision ID: 5d7e2a1c9f40
Revises: 4c1d816a0b2e
Create Date: 2026-10-26 01:30:00.000000

Nullable `insertions` and `deletions` per (article, path), counted from
the patch's diff hunks, so a cover's thread view can show its series'
"+N/-M" (#939). NULL means not counted yet, which differs from 0 (a
binary file, a pure rename): rows stored before this revision stay NULL
until `backfill-article-files --patch-subjects --reprocess` reaches
them. ADD COLUMN of a nullable column without a default is a
schema-only change in SQLite (no table rewrite), so the broker's
startup migration stays O(1) on the full archive. The downgrade's DROP
COLUMN does rewrite the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5d7e2a1c9f40"
down_revision: Union[str, Sequence[str], None] = "4c1d816a0b2e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("article_files", sa.Column("insertions", sa.Integer(), nullable=True))
    op.add_column("article_files", sa.Column("deletions", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("article_files", "deletions")
    op.drop_column("article_files", "insertions")
