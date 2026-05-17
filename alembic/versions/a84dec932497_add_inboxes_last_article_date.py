"""add inboxes.last_article_date

Revision ID: a84dec932497
Revises: bd2c6a41c4d5
Create Date: 2026-05-17 01:51:06.234546

Materialises the per-inbox "max article date" so the front-page
"Last activity" line doesn't ride the 24h `archive_stats` cache
window (see #216). Ingest bumps the column on every successful
commit; this migration backfills existing rows.

Backfill is a single correlated-MAX per inbox row, the same
per-inbox scan we currently pay on the 24h `archive_stats`
refresh. Once-only at upgrade time. Runs inside the scheduler
sidecar's migration step which gates the web tier on the
`/data/.migrated` sentinel, so the web container doesn't start
serving until the backfill is done.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a84dec932497'
down_revision: Union[str, Sequence[str], None] = 'bd2c6a41c4d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'inboxes', sa.Column('last_article_date', sa.DateTime(), nullable=True)
    )
    # Backfill from articles × article_lists. NULL stays NULL for
    # inboxes with no messages yet (the correlated SELECT returns
    # NULL on an empty set), which matches the column's runtime
    # semantics.
    op.execute(
        """
        UPDATE inboxes
        SET last_article_date = (
            SELECT MAX(a.date)
            FROM articles a
            JOIN article_lists al ON al.article_id = a.id
            WHERE al.inbox_id = inboxes.id
        )
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('inboxes', 'last_article_date')
