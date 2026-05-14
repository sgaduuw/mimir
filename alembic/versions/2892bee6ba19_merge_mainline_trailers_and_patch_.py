"""merge mainline-trailers and patch-series heads

Revision ID: 2892bee6ba19
Revises: 1c6472397d92, cfd9b25150b2
Create Date: 2026-05-14 20:26:16.639271

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2892bee6ba19'
down_revision: Union[str, Sequence[str], None] = ('1c6472397d92', 'cfd9b25150b2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
