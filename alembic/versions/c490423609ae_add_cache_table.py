"""add cache table

Revision ID: c490423609ae
Revises: c8e2a47f1d20
Create Date: 2026-05-04 22:17:53.480959

Replaces the on-disk pickle cache (`Settings.cache_path`) with a
DB-backed cache table. JSON values only; see `mimir.cache` for the
encoder/decoder. After upgrading, the old `mimir-cache.pickle*` files
are unreferenced and safe to delete.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c490423609ae'
down_revision: Union[str, Sequence[str], None] = 'c8e2a47f1d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'cache',
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_index(
        op.f('ix_cache_expires_at'), 'cache', ['expires_at'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_cache_expires_at'), table_name='cache')
    op.drop_table('cache')
