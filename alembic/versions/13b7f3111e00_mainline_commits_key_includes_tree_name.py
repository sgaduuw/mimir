"""mainline_commits key includes tree_name

Revision ID: 13b7f3111e00
Revises: 1072ad1fae96
Create Date: 2026-10-10 00:30:50.686982

The key was `(commit_sha, message_id)` and every walker insert is
`ON CONFLICT DO NOTHING`, so the first tree to record a commit owned
the row. A patch is normally applied in a subsystem tree before Linus
merges it with the same SHA, so the Linus insert was ignored and the
patch never showed as landed (#673). With `tree_name` in the key each
tree keeps its own row.

No data is changed here. The Linus rows that were ignored come back
with one `update-mainline --rewalk` after the deploy.

SQLite cannot change a primary key in place, so this is a table
rebuild: create a scratch table, copy, drop the old table (its indexes
go with it), rename, recreate both indexes. Cost on production is
unmeasured; it copies every `mainline_commits` row while holding the
writer at broker startup.

Interrupted runs follow `1072ad1fae96`, which documents why: the
scratch `CREATE TABLE` autocommits, so a killed run leaves it behind
and every retry would die on "table already exists". Debris is dropped
only while the live table still exists; if the scratch table is the
only copy, the migration refuses and names the one-statement recovery.

Downgrade keeps one row per `(commit_sha, message_id)`, preferring the
Linus row, which is the row the lifecycle pill depends on.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '13b7f3111e00'
down_revision: Union[str, Sequence[str], None] = '1072ad1fae96'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCRATCH = "_mainline_commits_rebuild"


def _drop_scratch_debris() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if SCRATCH not in tables:
        return
    if "mainline_commits" not in tables:
        raise RuntimeError(
            f"{SCRATCH} exists but mainline_commits does not. The scratch "
            "table is the ONLY copy of the data and must not be dropped. "
            "Recover with one statement:\n"
            f"  ALTER TABLE {SCRATCH} RENAME TO mainline_commits;\n"
            "Then restart. Do NOT create the indexes or run `alembic stamp` "
            "by hand: this migration recreates both indexes if_not_exists "
            "and stamps itself on the next run."
        )
    op.execute(f"DROP TABLE {SCRATCH}")


def _rebuild(primary_key: str, order_by: str) -> None:
    _drop_scratch_debris()
    op.execute(
        f"""
        CREATE TABLE {SCRATCH} (
            commit_sha VARCHAR NOT NULL,
            message_id VARCHAR NOT NULL,
            tree_name VARCHAR NOT NULL,
            committed_at DATETIME NOT NULL,
            PRIMARY KEY ({primary_key})
        )
        """
    )
    # OR IGNORE only matters on downgrade, where two trees' rows for one
    # commit collapse to one; ORDER BY decides which survives.
    op.execute(
        f"INSERT OR IGNORE INTO {SCRATCH} "
        "SELECT commit_sha, message_id, tree_name, committed_at "
        f"FROM mainline_commits ORDER BY {order_by}"
    )
    op.drop_table("mainline_commits")
    op.rename_table(SCRATCH, "mainline_commits")
    op.create_index(
        "ix_mainline_commits_message_id",
        "mainline_commits",
        ["message_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_mainline_commits_tree_name",
        "mainline_commits",
        ["tree_name"],
        if_not_exists=True,
    )


def upgrade() -> None:
    _rebuild("commit_sha, message_id, tree_name", "rowid")


def downgrade() -> None:
    _rebuild("commit_sha, message_id", "tree_name != 'linus', rowid")
