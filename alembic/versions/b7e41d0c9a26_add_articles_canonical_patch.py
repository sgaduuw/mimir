"""add articles.canonical_patch

Revision ID: b7e41d0c9a26
Revises: a3c1e5f7b9d2
Create Date: 2026-11-19 12:00:00.000000

#1221: recent postings called a Python SQL function per row. On
free-threaded CPython that function can free another thread's cached
statement while holding its own connection's mutex, and two broker
threads doing that to each other deadlock. The value is stored instead,
computed by `mimir.patch_subjects.canonical_patch` at every write of
subject, position or key.

The fill runs here, before the broker serves, because the query has no
fallback for unfilled rows: the only fallback is the Python function
this removes. ADD COLUMN with a default does not rewrite the table; the
fill walks id ranges and writes only the rows that are true.

Each batch commits on its own, so the WAL holds one batch, not the table.
A re-run is safe: the column is added only if missing, and the fill only
ever sets rows to 1.

Downgrade drops the column, which rewrites every row of `articles`.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from mimir.patch_subjects import canonical_patch_batches

# revision identifiers, used by Alembic.
revision: str = "b7e41d0c9a26"
down_revision: Union[str, Sequence[str], None] = "a3c1e5f7b9d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("articles")}
    if "canonical_patch" not in columns:
        op.add_column(
            "articles",
            sa.Column(
                "canonical_patch",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    update = sa.text("UPDATE articles SET canonical_patch = 1 WHERE id = :id")
    # One transaction per batch keeps the WAL to one batch's pages instead
    # of the whole table: the fill rewrites nearly every leaf page.
    with op.get_context().autocommit_block():
        for ids in canonical_patch_batches(bind):
            if ids:
                bind.exec_driver_sql("BEGIN")
                bind.execute(update, [{"id": i} for i in ids])
                bind.exec_driver_sql("COMMIT")


def downgrade() -> None:
    op.drop_column("articles", "canonical_patch")
