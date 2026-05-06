"""canonical-inbox + list-address auto-detection

Revision ID: 19d03ef76368
Revises: e1fa8344d719
Create Date: 2026-05-06 23:10:50.078785

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '19d03ef76368'
down_revision: Union[str, Sequence[str], None] = 'e1fa8344d719'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'inbox_address_observations',
        sa.Column('inbox_id', sa.Integer(), nullable=False),
        sa.Column('address', sa.String(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.Column('last_seen', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['inbox_id'], ['inboxes.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('inbox_id', 'address'),
    )

    # SQLite doesn't support ALTER TABLE ADD CONSTRAINT — use batch
    # mode (copy + move) to add the FK alongside the new column.
    with op.batch_alter_table('articles') as batch_op:
        batch_op.add_column(
            sa.Column('canonical_inbox_id', sa.Integer(), nullable=True)
        )
        batch_op.create_index(
            'ix_articles_canonical_inbox_id', ['canonical_inbox_id'], unique=False,
        )
        batch_op.create_foreign_key(
            'fk_articles_canonical_inbox_id_inboxes',
            'inboxes', ['canonical_inbox_id'], ['id'], ondelete='SET NULL',
        )

    with op.batch_alter_table('inboxes') as batch_op:
        batch_op.add_column(sa.Column('list_address', sa.String(), nullable=True))
        batch_op.create_index(
            'ix_inboxes_list_address', ['list_address'], unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('inboxes') as batch_op:
        batch_op.drop_index('ix_inboxes_list_address')
        batch_op.drop_column('list_address')

    with op.batch_alter_table('articles') as batch_op:
        batch_op.drop_constraint(
            'fk_articles_canonical_inbox_id_inboxes', type_='foreignkey',
        )
        batch_op.drop_index('ix_articles_canonical_inbox_id')
        batch_op.drop_column('canonical_inbox_id')

    op.drop_table('inbox_address_observations')
