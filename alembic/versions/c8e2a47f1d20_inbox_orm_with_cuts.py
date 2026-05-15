"""inbox ORM model + lean article columns

Revision ID: c8e2a47f1d20
Revises: d62891242107
Create Date: 2026-05-04 19:30:00.000000

Promotes inboxes to a first-class ORM model and slims `articles` to
the columns we actually query. After this revision:

  * `inboxes` table is the source-of-truth registry; bootstrapped from
    `Settings.inboxes` on app start, manageable from a future admin UI.
  * `article_lists` (join table) carries `(article_id, inbox_id,
    epoch, commit_sha)`, cross-posted messages dedupe to one Article
    + N ArticleList rows.
  * `articles` drops `epoch`, `commit_sha` (now per-inbox in
    `article_lists`), `in_reply_to` (redundant with `thread_parent`),
    and `references` (only consumed at ingest time, re-derivable from
    the blob).
  * `attachments` table is removed entirely, every render path
    re-parses the blob on demand and uses `parsed.attachments`, so
    nothing read the persisted metadata.
  * `ingest_state` PK becomes `(inbox_id, epoch)` and `last_ingested_at`
    is dropped (set but never read).

Squashed: replaces the unreleased 73c99bb5ef33 (denormalized
list_name) and a1c4f5e0d3b2 (intermediate normalization).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8e2a47f1d20'
down_revision: Union[str, Sequence[str], None] = 'd62891242107'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'inboxes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('mirror_path', sa.String(), nullable=False),
        sa.Column('upstream_url', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_inboxes_name'), 'inboxes', ['name'], unique=True)

    op.create_table(
        'article_lists',
        sa.Column('article_id', sa.Integer(), nullable=False),
        sa.Column('inbox_id', sa.Integer(), nullable=False),
        sa.Column('epoch', sa.String(), nullable=False),
        sa.Column('commit_sha', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['article_id'], ['articles.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['inbox_id'], ['inboxes.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('article_id', 'inbox_id'),
    )
    op.create_index(op.f('ix_article_lists_inbox_id'), 'article_lists', ['inbox_id'], unique=False)

    op.drop_table('attachments')

    # Slim articles: drop the columns we no longer query. Indexes on
    # dropped columns must be removed inside the same batch, alembic
    # otherwise tries to recreate them on the new (column-less) table.
    with op.batch_alter_table('articles') as batch:
        batch.drop_index(op.f('ix_articles_epoch'))
        batch.drop_index(op.f('ix_articles_in_reply_to'))
        batch.drop_column('epoch')
        batch.drop_column('commit_sha')
        batch.drop_column('in_reply_to')
        batch.drop_column('references')

    # ingest_state: PK changes, last_ingested_at goes away. recreate=
    # always since SQLite can't ALTER PRIMARY KEY in place. We pass an
    # explicit `copy_from` Table without a PrimaryKeyConstraint so the
    # source columns don't carry a column-level `primary_key=True`
    # flag, which would otherwise fight with the new composite PK and
    # raise an SAWarning ("future release may become exception").
    existing_ingest_state = sa.Table(
        'ingest_state',
        sa.MetaData(),
        sa.Column('epoch', sa.String(), nullable=False),
        sa.Column('last_commit_sha', sa.String(), nullable=True),
        sa.Column('last_ingested_at', sa.DateTime(), nullable=True),
    )
    with op.batch_alter_table(
        'ingest_state',
        copy_from=existing_ingest_state,
        recreate='always',
    ) as batch:
        batch.drop_column('last_ingested_at')
        batch.add_column(sa.Column('inbox_id', sa.Integer(), nullable=False))
        batch.create_foreign_key(
            'fk_ingest_state_inbox_id',
            'inboxes', ['inbox_id'], ['id'], ondelete='CASCADE',
        )
        batch.create_primary_key('pk_ingest_state', ['inbox_id', 'epoch'])

    # Low-cardinality `inbox_id` index needs sqlite_stat1 populated, or
    # the planner picks it over more selective indexes (thread_parent,
    # date) and turns thread reads into full-inbox scans.
    op.execute("ANALYZE")


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from a re-ingest if needed")
