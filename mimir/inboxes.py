"""Seed the `inboxes` table from the env-side inbox config.

`Settings.inboxes` is the *bootstrap* source: each entry guarantees an
`Inbox` row exists in the DB on first startup. After that, env entries
never overwrite the row — admin-UI edits to mirror_path / upstream_url
are preserved across restarts. To rotate a value via env, drop the row
manually (or do it from the admin UI).

Insert is `ON CONFLICT(name) DO NOTHING` so two workers cold-starting
in parallel can't trip the UNIQUE(name) constraint.
"""
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from mimir.config import settings
from mimir.models import Inbox


def bootstrap_inboxes(session: Session) -> dict[str, Inbox]:
    """Insert any missing `Settings.inboxes` entries, leave existing
    rows untouched, return {name: Inbox} for every row in the table.
    Safe to call repeatedly and concurrently."""
    rows = [
        {
            "name": name,
            "mirror_path": str(cfg.mirror_path),
            "upstream_url": cfg.upstream_url,
        }
        for name, cfg in settings.inboxes.items()
    ]
    if rows:
        stmt = sqlite_insert(Inbox).values(rows).on_conflict_do_nothing(
            index_elements=["name"]
        )
        session.execute(stmt)
        session.commit()

    return {
        inbox.name: inbox
        for inbox in session.execute(select(Inbox)).scalars().all()
    }
