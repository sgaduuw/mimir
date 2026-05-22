"""add robots_rules table

Revision ID: cc79e4a44a23
Revises: 7bbe216da110
Create Date: 2026-05-22 12:15:49.619581

Adds the `robots_rules` table that backs `/robots.txt` and the
`admin robots` CLI surface. Seeds the `*` stanza with the previous
hardcoded template's values (`crawl_delay=5,
disallow_paths=["/*/attachment/"]`) so the rendered output is
byte-identical immediately after `alembic upgrade head`.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cc79e4a44a23'
down_revision: Union[str, Sequence[str], None] = '7bbe216da110'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'robots_rules',
        sa.Column('user_agent', sa.String(), nullable=False),
        sa.Column('crawl_delay', sa.Integer(), nullable=True),
        sa.Column('disallow_paths', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('user_agent'),
    )
    op.execute(
        sa.text(
            "INSERT INTO robots_rules (user_agent, crawl_delay, disallow_paths) "
            "VALUES ('*', 5, :paths)"
        ).bindparams(paths=json.dumps(["/*/attachment/"]))
    )


def downgrade() -> None:
    op.drop_table('robots_rules')
