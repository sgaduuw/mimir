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

Production has 6.4M true rows among 18M, spread over the whole id range,
so the fill rewrites nearly every leaf page of `articles`. In one
transaction the WAL would hold all of them until the commit: on a 1M-row
synthetic table it peaked at the table's own size (302 MiB). Committing
each batch kept it at 18 MiB in the same time, so each batch is its own
transaction.

An interrupted run therefore leaves the column present, some batches
filled, and this revision unstamped. The column is added only when
missing, and the re-run walks every batch again. The fill only ever sets
rows to 1, so the rows a partial run set are a subset of the true ones.
A predicate changed between the two runs is not repaired here;
`backfill-patch-series` repairs it.

Cost, extrapolated rather than measured on production: the read side
took 24 s there (2026-11-19). On the 1M-row synthetic table the writes
took 1.6 s per 360k true rows, so about 30 to 45 s for 6.4M. Budget about
a minute in all, as a floor.

Downgrade drops the column, which rewrites every row of `articles` in
one transaction: 2.7 s per 1M synthetic rows, so roughly 50 to 75 s on
production (extrapolated), with the WAL growing to about the table's size.
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
