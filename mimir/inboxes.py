"""Seed the `inboxes` table from the env-side inbox config.

`Settings.inboxes` is the *bootstrap* source: each entry guarantees an
`Inbox` row exists in the DB on first startup. After that, env entries
never overwrite the row — admin-UI edits to mirror_path / upstream_url
are preserved across restarts. To rotate a value via env, drop the row
manually (or do it from the admin UI).

Insert is `ON CONFLICT(name) DO NOTHING` so two workers cold-starting
in parallel can't trip the UNIQUE(name) constraint.

A module-level `_INBOX_NAMES` cache avoids hitting the DB on every
request just to render the nav. It's repopulated by `bootstrap_inboxes`
on every startup; an admin-UI add/remove can call `refresh_inbox_names`
to invalidate it.
"""
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import Inbox

# Replaced atomically via list slice-assignment; readers see either
# the old list or the new one, never a partial state.
_INBOX_NAMES: list[str] = []


def refresh_inbox_names() -> list[str]:
    """Repopulate the cached nav list from the DB. Returns the new list."""
    with SessionLocal() as session:
        names = sorted(n for n, in session.execute(select(Inbox.name)))
    _INBOX_NAMES[:] = names
    return list(names)


def inbox_names() -> list[str]:
    """The current set of inbox slugs, for nav rendering. Cheap — no DB hit."""
    return list(_INBOX_NAMES)


def bootstrap_inboxes() -> dict[str, Inbox]:
    """Insert any missing `Settings.inboxes` entries, leave existing
    rows untouched, return {name: Inbox} for every row in the table.
    Safe to call repeatedly and concurrently.

    Owns its own session so callers can't accidentally have unrelated
    pending writes committed alongside the bootstrap. Returned Inbox
    instances are detached; pass them through `session.merge()` if you
    need them attached to a working session.
    """
    rows = [
        {
            "name": name,
            "mirror_path": str(cfg.mirror_path),
            "upstream_url": cfg.upstream_url,
        }
        for name, cfg in settings.inboxes.items()
    ]
    with SessionLocal() as session:
        if rows:
            stmt = sqlite_insert(Inbox).values(rows).on_conflict_do_nothing(
                index_elements=["name"]
            )
            session.execute(stmt)
            session.commit()

        out = {
            inbox.name: inbox
            for inbox in session.execute(select(Inbox)).scalars().all()
        }
    _INBOX_NAMES[:] = sorted(out.keys())
    return out
