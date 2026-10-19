"""unique inboxes.list_address

Revision ID: 9b2878b4f911
Revises: 1496642cb923
Create Date: 2026-10-18 22:45:00.000000

Canonical resolution maps one list address to one inbox, so a second
holder pins cross-posts to the wrong list (#644). `admin inbox update`
and auto-promotion already refuse a held address; this makes the
database refuse it too, behind them (#658). NULL means "not detected
yet" and stays shared, hence the partial index.

Replaces the plain `ix_inboxes_list_address` under the same name.
CREATE and DROP INDEX are native SQLite DDL, so no table rebuild.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9b2878b4f911"
down_revision: Union[str, Sequence[str], None] = "1496642cb923"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Checked first so a broker start fails naming what to fix, rather
    # than on a bare IntegrityError after the plain index is gone.
    duplicates = op.get_bind().execute(
        sa.text(
            "SELECT list_address, group_concat(name, ', ') FROM "
            "(SELECT list_address, name FROM inboxes "
            " WHERE list_address IS NOT NULL ORDER BY list_address, name) "
            "GROUP BY list_address HAVING count(*) > 1"
        )
    ).all()
    if duplicates:
        held = "\n".join(f"  {addr} is held by {names}" for addr, names in duplicates)
        raise RuntimeError(
            "inboxes.list_address must be unique (#658), but these addresses "
            "are held by more than one inbox:\n"
            f"{held}\n"
            "Nothing was changed. On the previous release, clear the extra "
            "holders with `mimir admin inbox update NAME --list-address \"\"`, "
            "then upgrade again."
        )

    # if_exists: this DDL runs outside a transaction, so a run killed
    # between the drop and the create restarts with no index to drop.
    op.drop_index("ix_inboxes_list_address", table_name="inboxes", if_exists=True)
    op.create_index(
        "ix_inboxes_list_address",
        "inboxes",
        ["list_address"],
        unique=True,
        sqlite_where=sa.text("list_address IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_inboxes_list_address", table_name="inboxes")
    op.create_index(
        "ix_inboxes_list_address", "inboxes", ["list_address"], unique=False
    )
