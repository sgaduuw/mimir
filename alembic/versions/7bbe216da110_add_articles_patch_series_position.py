"""add articles.patch_series_position

Revision ID: 7bbe216da110
Revises: a84dec932497
Create Date: 2026-05-17 02:12:03.220053

#212: persist per-patch position within a series so the inter-
revision diff route can do an indexed lookup instead of walking
each cover letter's thread children at request time.

Schema:
- NULL = not a series patch (replies, prose, solo `[PATCH]`,
  `[GIT PULL]`, etc.)
- 0    = cover letter (`[PATCH ... 0/N]`)
- 1+   = in-series patch position (the `M` of `[PATCH ... M/T]`)

Backfill posture: this migration sets position=0 on every article
that already has `patch_series_key` set, those are cover letters
(Slice 1 only wrote the key on covers). In-series patches stay
NULL here and are filled by the per-article `backfill-patch-series`
command's extended pass once it's reprocessed against the live
data; cheap because thread-parent lookup needs the same row set
the backfill walks already.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7bbe216da110'
down_revision: Union[str, Sequence[str], None] = 'a84dec932497'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'articles',
        sa.Column('patch_series_position', sa.Integer(), nullable=True),
    )
    # Existing cover letters are the only articles with
    # patch_series_key set today (Slice 1); mark them as position=0
    # so the state card's same-position filter (added in this PR)
    # keeps working for them without waiting for the backfill
    # command. Indexed scan via ix_articles_patch_series_key; fast.
    op.execute(
        """
        UPDATE articles
        SET patch_series_position = 0
        WHERE patch_series_key IS NOT NULL
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('articles', 'patch_series_position')
