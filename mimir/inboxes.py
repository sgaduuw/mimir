"""Reconcile the env-side inbox config (`Settings.inboxes`) with the
`inboxes` table.

`Settings.inboxes` is the bootstrap source: each entry guarantees an
`Inbox` row exists in the DB, with `mirror_path` / `upstream_url`
synced to the env value. A future admin UI can add inboxes that don't
appear in env; those are left alone here.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from mimir.config import settings
from mimir.models import Inbox


def bootstrap_inboxes(session: Session) -> dict[str, Inbox]:
    """Upsert every `Settings.inboxes` entry into the DB and return a
    {name: Inbox} map covering at least those entries. Safe to call
    repeatedly; idempotent."""
    existing: dict[str, Inbox] = {
        inbox.name: inbox
        for inbox in session.execute(select(Inbox)).scalars().all()
    }

    dirty = False
    for name, cfg in settings.inboxes.items():
        mirror_path_str = str(cfg.mirror_path)
        inbox = existing.get(name)
        if inbox is None:
            inbox = Inbox(
                name=name,
                mirror_path=mirror_path_str,
                upstream_url=cfg.upstream_url,
            )
            session.add(inbox)
            existing[name] = inbox
            dirty = True
        else:
            if inbox.mirror_path != mirror_path_str or inbox.upstream_url != cfg.upstream_url:
                inbox.mirror_path = mirror_path_str
                inbox.upstream_url = cfg.upstream_url
                dirty = True

    if dirty:
        session.commit()

    return existing
