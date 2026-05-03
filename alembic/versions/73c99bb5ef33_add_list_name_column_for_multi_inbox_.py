"""add list_name column for multi-inbox support

Revision ID: 73c99bb5ef33
Revises: d62891242107
Create Date: 2026-05-03 23:07:05.372805

Adds Article.list_name (indexed, default "lkml") and changes
IngestState's primary key from (epoch) to (list_name, epoch) so two
inboxes can each have their own per-epoch ingest cursor without
colliding. Existing rows backfill to "lkml" via the server_default.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '73c99bb5ef33'
down_revision: Union[str, Sequence[str], None] = 'd62891242107'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'articles',
        sa.Column('list_name', sa.String(), nullable=False, server_default='lkml'),
    )
    op.create_index(op.f('ix_articles_list_name'), 'articles', ['list_name'], unique=False)

    # IngestState gets a new column AND a new composite primary key.
    # SQLite can't ALTER PRIMARY KEY in place; batch_alter_table
    # recreates the table, copying rows.
    with op.batch_alter_table('ingest_state', recreate='always') as batch:
        batch.add_column(
            sa.Column('list_name', sa.String(), nullable=False, server_default='lkml')
        )
        batch.create_primary_key('pk_ingest_state', ['list_name', 'epoch'])

    # The new list_name index has very low cardinality (one entry per
    # configured inbox). Without ANALYZE, SQLite's default query
    # planner picks ix_articles_list_name for joins like
    # `JOIN articles ON thread_parent = ... WHERE list_name = ?`,
    # which then scans every row in the inbox per recursion step.
    # ANALYZE updates sqlite_stat1 so the planner uses the much more
    # selective ix_articles_thread_parent instead. ~50s on 6.2M rows.
    op.execute("ANALYZE")


def downgrade() -> None:
    with op.batch_alter_table('ingest_state', recreate='always') as batch:
        batch.drop_column('list_name')
        batch.create_primary_key('pk_ingest_state', ['epoch'])
    op.drop_index(op.f('ix_articles_list_name'), table_name='articles')
    op.drop_column('articles', 'list_name')
