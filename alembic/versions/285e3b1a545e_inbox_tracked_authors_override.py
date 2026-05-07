"""inbox tracked_authors override

Revision ID: 285e3b1a545e
Revises: 19d03ef76368
Create Date: 2026-05-07 23:23:39.910547

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '285e3b1a545e'
down_revision: Union[str, Sequence[str], None] = '19d03ef76368'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('inboxes') as batch_op:
        batch_op.add_column(sa.Column('tracked_authors', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('inboxes') as batch_op:
        batch_op.drop_column('tracked_authors')
