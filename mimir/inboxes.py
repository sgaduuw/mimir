"""Inbox bootstrap + admin service layer.

`Settings.inboxes` is the *bootstrap* source: each entry guarantees an
`Inbox` row exists in the DB on first startup. After that, env entries
never overwrite the row — admin edits to mirror_path / upstream_url
are preserved across restarts. To rotate a value via env, drop the row
through `delete_inbox()` first.

Insert is `ON CONFLICT(name) DO NOTHING` so two workers cold-starting
in parallel can't trip the UNIQUE(name) constraint.

A module-level `_INBOX_NAMES` cache avoids hitting the DB on every
request just to render the nav. It's repopulated by `bootstrap_inboxes`
on every startup; CRUD ops in this module call `_publish_names()` so
the cache is consistent with the DB after every change.

The CRUD service functions (`create_inbox`, `update_inbox`,
`delete_inbox`) and the per-inbox tracker mutators (`set_tracked_authors`,
`add_tracked_author`, `remove_tracked_author`, `clear_tracked_authors`)
are shared between the CLI admin commands and the future Flask admin
UI — keeps validation in one place.
"""
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import delete, exists, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from mimir import cache
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, Inbox, IngestState

logger = logging.getLogger(__name__)

# Slug constraint: name flows into URL paths and cache-key fragments,
# so it must be URL-safe and `:`-free. Lowercase, alphanumeric +
# hyphens, doesn't start or end with a hyphen. ≤64 chars.
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

# Replaced atomically via list slice-assignment; readers see either
# the old list or the new one, never a partial state.
_INBOX_NAMES: list[str] = []


class InboxValidationError(ValueError):
    """Raised when input to a CRUD function fails validation. The
    message is suitable for display to the operator."""


class InboxNotFound(LookupError):
    """Raised when a CRUD op references an Inbox that doesn't exist."""


def validate_name(name: str) -> str:
    """Normalize and validate an inbox name. Returns the normalized
    name. Raises InboxValidationError on bad input."""
    if not isinstance(name, str):
        raise InboxValidationError("name must be a string")
    name = name.strip()
    if not _NAME_RE.fullmatch(name):
        raise InboxValidationError(
            f"name {name!r} must be lowercase alphanumeric/hyphen, "
            "1–64 chars, not starting or ending with a hyphen"
        )
    return name


def validate_upstream_url(url: str) -> str:
    """Validate an upstream public-inbox URL. Returns the (stripped)
    URL. Raises InboxValidationError on bad input."""
    if not isinstance(url, str):
        raise InboxValidationError("upstream_url must be a string")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise InboxValidationError(
            f"upstream_url must use https:// (got {parsed.scheme!r})"
        )
    if not parsed.netloc:
        raise InboxValidationError("upstream_url must include a host")
    return url


_TRACKER_LABEL_MAX = 64
_TRACKER_SUBSTRING_MAX = 256


def validate_tracked_authors(
    authors: dict[str, str] | None,
) -> dict[str, str] | None:
    """Validate a tracked-authors dict. Returns the (stripped) dict, or
    None if input was None or empty (the two states collapse — both
    mean "no tracker tiles for this inbox"). Raises
    InboxValidationError on bad input."""
    if authors is None:
        return None
    if not isinstance(authors, dict):
        raise InboxValidationError("tracked_authors must be a dict")
    cleaned: dict[str, str] = {}
    for label, substring in authors.items():
        if not isinstance(label, str) or not isinstance(substring, str):
            raise InboxValidationError(
                "tracked_authors keys and values must be strings"
            )
        label = label.strip()
        substring = substring.strip()
        if not label:
            raise InboxValidationError("tracker label must not be empty")
        if not substring:
            raise InboxValidationError(
                f"tracker substring for {label!r} must not be empty"
            )
        if len(label) > _TRACKER_LABEL_MAX:
            raise InboxValidationError(
                f"tracker label {label!r} exceeds {_TRACKER_LABEL_MAX} chars"
            )
        if len(substring) > _TRACKER_SUBSTRING_MAX:
            raise InboxValidationError(
                f"tracker substring for {label!r} exceeds "
                f"{_TRACKER_SUBSTRING_MAX} chars"
            )
        cleaned[label] = substring
    return cleaned or None


def validate_mirror_path(mirror_path: str) -> str:
    """Validate a mirror_path string. Returns the stripped value.

    No filesystem checks here — the directory is allowed to not exist
    yet; `flask --app mimir update` will create it on first clone.
    Path-traversal validation belongs at the web-input boundary
    (deferred admin-UI task) where the value is untrusted.
    """
    if not isinstance(mirror_path, str):
        raise InboxValidationError("mirror_path must be a string")
    mirror_path = mirror_path.strip()
    if not mirror_path:
        raise InboxValidationError("mirror_path must not be empty")
    return mirror_path


def _publish_names(session: Session) -> None:
    """Refresh the nav-name cache from the open session. Use after
    any CRUD op so the running web process sees the change."""
    names = sorted(n for n, in session.execute(select(Inbox.name)))
    _INBOX_NAMES[:] = names


def refresh_inbox_names() -> list[str]:
    """Repopulate the cached nav list from the DB. Returns the new list."""
    with SessionLocal() as session:
        _publish_names(session)
    return list(_INBOX_NAMES)


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
        _publish_names(session)
    return out


def list_inboxes() -> list[Inbox]:
    """Every inbox in the DB, ordered by name. Detached from the
    transient session — pass through `session.merge()` if needed."""
    with SessionLocal() as session:
        return list(
            session.execute(select(Inbox).order_by(Inbox.name)).scalars().all()
        )


def get_inbox(name: str) -> Inbox:
    """Fetch one inbox by name. Raises InboxNotFound if absent."""
    with SessionLocal() as session:
        inbox = session.execute(
            select(Inbox).where(Inbox.name == name)
        ).scalar_one_or_none()
    if inbox is None:
        raise InboxNotFound(f"no inbox named {name!r}")
    return inbox


def create_inbox(name: str, mirror_path: str, upstream_url: str) -> Inbox:
    """Insert a new Inbox after validating all three fields. Raises
    InboxValidationError on bad input or if the name is already in use."""
    name = validate_name(name)
    mirror_path = validate_mirror_path(mirror_path)
    upstream_url = validate_upstream_url(upstream_url)

    with SessionLocal() as session:
        if session.execute(select(Inbox).where(Inbox.name == name)).first() is not None:
            raise InboxValidationError(f"inbox {name!r} already exists")
        inbox = Inbox(name=name, mirror_path=mirror_path, upstream_url=upstream_url)
        session.add(inbox)
        session.commit()
        session.refresh(inbox)
        _publish_names(session)
        session.expunge(inbox)
    return inbox


def update_inbox(
    name: str,
    *,
    new_name: str | None = None,
    mirror_path: str | None = None,
    upstream_url: str | None = None,
) -> Inbox:
    """Modify an existing inbox. Only the fields passed in are
    touched. Raises InboxNotFound / InboxValidationError as
    appropriate."""
    if new_name is not None:
        new_name = validate_name(new_name)
    if mirror_path is not None:
        mirror_path = validate_mirror_path(mirror_path)
    if upstream_url is not None:
        upstream_url = validate_upstream_url(upstream_url)

    with SessionLocal() as session:
        inbox = session.execute(
            select(Inbox).where(Inbox.name == name)
        ).scalar_one_or_none()
        if inbox is None:
            raise InboxNotFound(f"no inbox named {name!r}")

        old_name = inbox.name
        renamed = False
        if new_name is not None and new_name != inbox.name:
            collision = session.execute(
                select(Inbox).where(Inbox.name == new_name)
            ).first()
            if collision is not None:
                raise InboxValidationError(f"inbox {new_name!r} already exists")
            inbox.name = new_name
            renamed = True
        if mirror_path is not None:
            inbox.mirror_path = mirror_path
        if upstream_url is not None:
            inbox.upstream_url = upstream_url
        session.commit()
        session.refresh(inbox)
        _publish_names(session)
        session.expunge(inbox)

    # Drop cached entries that referenced the old name. Cache values
    # don't depend on mirror_path / upstream_url, so non-rename
    # updates don't need invalidation.
    if renamed:
        cache.delete_for_inbox(old_name)
    return inbox


class InboxRemovalReport:
    """What `delete_inbox()` actually did, for the CLI / admin UI to
    report back to the operator."""
    __slots__ = ("name", "article_lists_deleted", "ingest_state_deleted",
                 "orphan_articles_deleted", "mirror_path_deleted")

    def __init__(self, name: str):
        self.name = name
        self.article_lists_deleted = 0
        self.ingest_state_deleted = 0
        self.orphan_articles_deleted = 0
        self.mirror_path_deleted: str | None = None


def delete_inbox(
    name: str,
    *,
    keep_orphan_articles: bool = False,
    remove_inbox_data: bool = False,
) -> InboxRemovalReport:
    """Remove an inbox and its dependent rows.

    The FKs cascade-delete `article_lists` and `ingest_state` rows.
    By default, also deletes any `articles` left without remaining
    `article_lists` rows (orphans). Pass `keep_orphan_articles=True`
    to keep them in place — useful if you plan to re-add the inbox.

    `remove_inbox_data=True` additionally `rm -rf`s the on-disk
    public-inbox mirror at `mirror_path`. The caller is responsible
    for confirming with the operator first; this function never
    prompts.
    """
    report = InboxRemovalReport(name)
    with SessionLocal() as session:
        inbox = session.execute(
            select(Inbox).where(Inbox.name == name)
        ).scalar_one_or_none()
        if inbox is None:
            raise InboxNotFound(f"no inbox named {name!r}")

        mirror_path_str = inbox.mirror_path

        report.article_lists_deleted = session.execute(
            select(func.count()).select_from(ArticleList)
            .where(ArticleList.inbox_id == inbox.id)
        ).scalar_one() or 0
        report.ingest_state_deleted = session.execute(
            select(func.count()).select_from(IngestState)
            .where(IngestState.inbox_id == inbox.id)
        ).scalar_one() or 0

        # Cascade-delete via the FK ondelete='CASCADE' on
        # article_lists.inbox_id and ingest_state.inbox_id.
        session.delete(inbox)
        session.flush()

        if not keep_orphan_articles:
            orphan_stmt = delete(Article).where(
                ~exists().where(ArticleList.article_id == Article.id)
            )
            report.orphan_articles_deleted = session.execute(orphan_stmt).rowcount or 0

        session.commit()
        _publish_names(session)

    cache.delete_for_inbox(name)

    if remove_inbox_data:
        # Resolve relative to project root if not absolute, the same
        # way the rest of the code does.
        path = Path(mirror_path_str)
        # Promote to the parent ONLY when the layout exactly matches
        # the bootstrap convention: `<anything>/<name>/git`. Then the
        # parent's basename is the inbox name, and removing the wrapper
        # `<name>/` directory is the right "leave no empty husk behind"
        # behaviour. Any other shape (operator-supplied
        # `mirror_path=/some/dir/git`, custom layouts, etc.) stays on
        # the literal mirror_path — promoting to the parent there would
        # rm-rf an unrelated directory that the operator never asked us
        # to touch. The audit on 2026-05-15 flagged the unconditional
        # `path.name == "git"` promotion as the worst data-loss vector
        # in the codebase.
        target = path
        if path.name == "git" and path.parent.name == name:
            target = path.parent
        logger.warning(
            "delete_inbox(%s): rmtree target resolved to %s "
            "(from mirror_path=%s)",
            name, target, mirror_path_str,
        )
        if target.exists():
            shutil.rmtree(target)
            report.mirror_path_deleted = str(target)

    return report


def set_tracked_authors(
    name: str, authors: dict[str, str] | None,
) -> Inbox:
    """Replace an inbox's tracked-authors dict. `authors=None` (or an
    empty dict) writes NULL — the dashboard renders no tracker tiles
    for that inbox. Raises InboxNotFound / InboxValidationError as
    appropriate.

    No cache invalidation: `author_recent` cache keys embed the email
    substring, not the label, so adding/removing a tracker doesn't
    invalidate any existing key — orphan rows age out via TTL.
    """
    cleaned = validate_tracked_authors(authors)
    with SessionLocal() as session:
        inbox = session.execute(
            select(Inbox).where(Inbox.name == name)
        ).scalar_one_or_none()
        if inbox is None:
            raise InboxNotFound(f"no inbox named {name!r}")
        inbox.tracked_authors = cleaned
        session.commit()
        session.refresh(inbox)
        session.expunge(inbox)
    return inbox


def add_tracked_author(name: str, label: str, substring: str) -> Inbox:
    """Add or replace one tracker entry. If the inbox has no trackers
    yet, this initializes the dict with the single entry."""
    inbox = get_inbox(name)
    current = dict(inbox.tracked_authors or {})
    current[label] = substring
    return set_tracked_authors(name, current)


def remove_tracked_author(name: str, label: str) -> Inbox:
    """Remove one tracker entry by label. Raises InboxValidationError
    if the label isn't present. Removing the last entry leaves the
    column NULL (no tracker tiles)."""
    inbox = get_inbox(name)
    current = dict(inbox.tracked_authors or {})
    if label not in current:
        raise InboxValidationError(
            f"inbox {name!r} has no tracker labelled {label!r}"
        )
    del current[label]
    return set_tracked_authors(name, current or None)


def clear_tracked_authors(name: str) -> Inbox:
    """Drop all tracker entries on an inbox (writes NULL)."""
    return set_tracked_authors(name, None)
