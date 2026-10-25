"""Manage the Inbox entity's lifecycle: bootstrap from env, mutate
via admin.

Two sub-areas, one concern (the `Inbox` row, end to end):

- **Bootstrap.** `Settings.inboxes` is the bootstrap source:
  each entry guarantees an `Inbox` row exists in the DB on first
  startup. After that, env entries never overwrite the row, admin
  edits to mirror_path / upstream_url are preserved across
  restarts. To rotate a value via env, drop the row through
  `delete_inbox()` first. Insert is `ON CONFLICT(name) DO NOTHING`
  so two workers cold-starting in parallel can't trip the
  `UNIQUE(name)` constraint.
- **Mutate.** The CRUD service functions (`create_inbox`,
  `update_inbox`, `delete_inbox`) and the per-inbox tracker
  mutator (`set_tracked_authors`) are shared between the CLI admin
  commands and the future Flask admin UI. Keeps validation in one
  place. Every mutator writes through the broker's WriterThread
  and raises `RuntimeError` when none is active.

Validators (`validate_name`, `validate_upstream_url`,
`validate_mirror_path`, `validate_tracked_authors`) are pure and
shared by both sub-areas, splitting them out would scatter
tightly-coupled state for no real win. See #187 for the won't-split
reasoning.
"""

import logging
import shutil
from email.utils import parseaddr
from pathlib import Path

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mimir import cache
from mimir._inbox_name import INBOX_NAME_RE
from mimir._outbound import OutboundUrlError, validate_outbound_url
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, Inbox, IngestState

logger = logging.getLogger(__name__)


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
    if not INBOX_NAME_RE.fullmatch(name):
        raise InboxValidationError(
            f"name {name!r} must be lowercase alphanumeric/hyphen, "
            "1 64 chars, not starting or ending with a hyphen"
        )
    return name


def validate_upstream_url(url: str) -> str:
    """Validate an upstream public-inbox URL. Returns the (stripped)
    URL. Raises InboxValidationError on bad input.

    Delegates to `mimir._outbound.validate_outbound_url` for the
    scheme + host + IP-literal checks (loopback, link-local incl.
    cloud-metadata, RFC 1918, etc.) so the admin surface, the
    Settings field, and the fetch path share one validation
    definition. We re-raise OutboundUrlError as InboxValidationError
    so existing admin-CLI handlers keep working unchanged.
    """
    try:
        return validate_outbound_url(url, allow_http=False)
    except OutboundUrlError as exc:
        raise InboxValidationError(f"upstream_url: {exc}") from exc


_TRACKER_LABEL_MAX = 64
_TRACKER_SUBSTRING_MAX = 256


def validate_tracked_authors(
    authors: dict[str, str] | None,
) -> dict[str, str] | None:
    """Validate a tracked-authors dict. Returns the (stripped) dict, or
    None if input was None or empty (the two states collapse, both
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

    No filesystem checks here, the directory is allowed to not exist
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


def bootstrap_inboxes() -> dict[str, Inbox]:
    """Insert any missing `Settings.inboxes` entries, leave existing
    rows untouched, return {name: Inbox} for every row in the table.
    Safe to call repeatedly and concurrently.

    Phase 6a: the write dispatches through the active WriterThread when
    a broker context is set (the steady-state `handle_bootstrap_inboxes`
    RPC); otherwise it runs on `SessionLocal` (the pre-serve
    `_bootstrap_inboxes_if_needed`, exception class 2, and non-broker
    callers / tests). The re-read runs on a read session after the
    write commits.
    """
    rows = [
        {
            "name": name,
            "mirror_path": str(cfg.mirror_path),
            "upstream_url": cfg.upstream_url,
        }
        for name, cfg in settings.inboxes.items()
    ]

    try:
        from mimir.broker import _context

        writer = _context.get_active_writer()
    except RuntimeError:
        writer = None

    if rows:
        if writer is not None:
            from mimir.broker.writes import WriteOp

            def _fn(conn):
                conn.execute(
                    sqlite_insert(Inbox)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["name"])
                )

            writer.submit(WriteOp(label="bootstrap_inboxes", fn=_fn)).result()
        else:
            with SessionLocal() as session:
                session.execute(
                    sqlite_insert(Inbox)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["name"])
                )
                session.commit()

    with SessionLocal() as session:
        return {
            inbox.name: inbox
            for inbox in session.execute(select(Inbox)).scalars().all()
        }


def list_inboxes() -> list[Inbox]:
    """Every inbox in the DB, ordered by name. Detached from the
    transient session, pass through `session.merge()` if needed."""
    with SessionLocal() as session:
        return list(session.execute(select(Inbox).order_by(Inbox.name)).scalars().all())


def pinned_first(inboxes: list[Inbox]) -> list[Inbox]:
    """`inboxes` with the pinned ones first, in `settings.pinned_inboxes`
    order, then the rest by name. The one definition of "pinned order",
    shared by the meta-index and `warm-cache`, so the cards topping the
    front page are also the inboxes warmed first (#763). A name listed
    twice keeps its first position."""
    rank = {name: i for i, name in enumerate(dict.fromkeys(settings.pinned_inboxes))}
    return sorted(inboxes, key=lambda ix: (rank.get(ix.name, len(rank)), ix.name))


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

    from mimir.broker.writes import submit_write

    def _fn(conn):
        if conn.execute(select(Inbox).where(Inbox.name == name)).first() is not None:
            raise InboxValidationError(f"inbox {name!r} already exists")
        conn.execute(
            sqlite_insert(Inbox).values(
                name=name,
                mirror_path=mirror_path,
                upstream_url=upstream_url,
            )
        )
        row = conn.execute(select(Inbox).where(Inbox.name == name)).one()
        return Inbox(**dict(row._mapping))

    return submit_write(f"inbox:create:{name}", _fn, timeout=30)


def _check_list_address_free(conn, list_address: str, inbox_id: int) -> None:
    """Refuse a `list_address` another inbox already holds. Canonical
    resolution maps one address to one inbox, so a duplicate pins
    cross-posts to the wrong list (#644). `conn` is a Connection or a
    Session; both expose `execute`."""
    holder = conn.execute(
        select(Inbox.name).where(
            Inbox.list_address == list_address, Inbox.id != inbox_id
        )
    ).scalar()
    if holder is not None:
        raise InboxValidationError(
            f"list_address {list_address!r} is already held by {holder!r}"
        )


def update_inbox(
    name: str,
    *,
    new_name: str | None = None,
    mirror_path: str | None = None,
    upstream_url: str | None = None,
    list_address: str | None = None,
) -> Inbox:
    """Modify an existing inbox. Only the fields passed in are
    touched. `list_address=""` clears it to NULL; any other value is
    stored stripped and lowercased, as `extract_list_addresses` and the
    observation tally store them. Raises InboxNotFound /
    InboxValidationError as appropriate."""
    if new_name is not None:
        new_name = validate_name(new_name)
    if mirror_path is not None:
        mirror_path = validate_mirror_path(mirror_path)
    if upstream_url is not None:
        upstream_url = validate_upstream_url(upstream_url)
    # "" is the clear sentinel (stored as NULL), so only None means
    # "leave alone".
    if list_address is not None:
        list_address = list_address.strip().lower()
        if list_address and (
            "@" not in list_address or parseaddr(list_address)[1] != list_address
        ):
            raise InboxValidationError(
                f"list_address: {list_address!r} is not a bare address"
            )

    from mimir.broker.writes import submit_write

    def _fn(conn) -> tuple[Inbox, str | None]:
        row = conn.execute(select(Inbox).where(Inbox.name == name)).first()
        if row is None:
            raise InboxNotFound(f"no inbox named {name!r}")

        old_name_val = row.name
        effective_name = old_name_val
        values: dict = {}
        renamed_from: str | None = None

        if new_name is not None and new_name != old_name_val:
            collision = conn.execute(
                select(Inbox).where(Inbox.name == new_name)
            ).first()
            if collision is not None:
                raise InboxValidationError(f"inbox {new_name!r} already exists")
            values["name"] = new_name
            effective_name = new_name
            # Returning `renamed_from` to the caller via the WriteFuture
            # rather than a side-channel captured dict: the future's
            # internal Condition gives a well-defined memory-ordering
            # boundary, so the writer thread's write is reliably
            # visible to the caller thread after `.result()`.
            renamed_from = old_name_val

        if mirror_path is not None:
            values["mirror_path"] = mirror_path
        if upstream_url is not None:
            values["upstream_url"] = upstream_url
        if list_address is not None:
            if list_address:
                _check_list_address_free(conn, list_address, row.id)
            values["list_address"] = list_address or None

        if values:
            conn.execute(update(Inbox).where(Inbox.name == name).values(**values))
        updated = conn.execute(select(Inbox).where(Inbox.name == effective_name)).one()
        return Inbox(**dict(updated._mapping)), renamed_from

    result, renamed_from = submit_write(f"inbox:update:{name}", _fn, timeout=30)
    # Drop cached entries that referenced the old name. Cache values
    # don't depend on mirror_path / upstream_url, so non-rename
    # updates don't need invalidation. This stays OUTSIDE the closure
    # because cache.delete_for_inbox is not a DB op (Decision 10).
    if renamed_from is not None:
        cache.delete_for_inbox(renamed_from)
    return result


class InboxRemovalReport:
    """What `delete_inbox()` actually did, for the CLI / admin UI to
    report back to the operator."""

    __slots__ = (
        "article_lists_deleted",
        "ingest_state_deleted",
        "mirror_path_deleted",
        "name",
        "orphan_articles_deleted",
    )

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
    to keep them in place, useful if you plan to re-add the inbox.

    `remove_inbox_data=True` additionally `rm -rf`s the on-disk
    public-inbox mirror at `mirror_path`. The caller is responsible
    for confirming with the operator first; this function never
    prompts.
    """
    report = InboxRemovalReport(name)

    from mimir.broker.writes import submit_write

    def _fn(conn) -> tuple[int, int, int, str]:
        row = conn.execute(select(Inbox).where(Inbox.name == name)).first()
        if row is None:
            raise InboxNotFound(f"no inbox named {name!r}")

        inbox_id = row.id
        mirror_path_val = row.mirror_path

        article_lists_count = (
            conn.execute(
                select(func.count())
                .select_from(ArticleList)
                .where(ArticleList.inbox_id == inbox_id)
            ).scalar_one()
            or 0
        )
        ingest_state_count = (
            conn.execute(
                select(func.count())
                .select_from(IngestState)
                .where(IngestState.inbox_id == inbox_id)
            ).scalar_one()
            or 0
        )

        # Cascade-delete via the FK ondelete='CASCADE' on
        # article_lists.inbox_id and ingest_state.inbox_id.
        conn.execute(delete(Inbox).where(Inbox.name == name))

        orphan_articles_count = 0
        if not keep_orphan_articles:
            orphan_stmt = delete(Article).where(
                ~exists().where(ArticleList.article_id == Article.id)
            )
            orphan_articles_count = conn.execute(orphan_stmt).rowcount or 0

        # Return the counts AND the mirror_path so the caller can
        # populate the report and (optionally) rmtree the on-disk
        # mirror. Returning rather than side-channelling through a
        # captured dict because the WriteFuture's internal Condition
        # gives a well-defined memory-ordering boundary.
        return (
            article_lists_count,
            ingest_state_count,
            orphan_articles_count,
            mirror_path_val,
        )

    al_count, is_count, orphan_count, mirror_path_str = submit_write(
        f"inbox:delete:{name}", _fn, timeout=30
    )
    report.article_lists_deleted = al_count
    report.ingest_state_deleted = is_count
    report.orphan_articles_deleted = orphan_count

    # cache.delete_for_inbox stays OUTSIDE the closure (Decision 10).
    cache.delete_for_inbox(name)

    if remove_inbox_data:
        path = Path(mirror_path_str)
        target = path
        if path.name == "git" and path.parent.name == name:
            target = path.parent
        logger.warning(
            "delete_inbox(%s): rmtree target resolved to %s (from mirror_path=%s)",
            name,
            target,
            mirror_path_str,
        )
        if target.exists():
            shutil.rmtree(target)
            report.mirror_path_deleted = str(target)

    return report


def set_tracked_authors(
    name: str,
    authors: dict[str, str] | None,
) -> Inbox:
    """Replace an inbox's tracked-authors dict. `authors=None` (or an
    empty dict) writes NULL, the dashboard renders no tracker tiles
    for that inbox. Raises InboxNotFound / InboxValidationError as
    appropriate.

    No cache invalidation: `author_recent` cache keys embed the email
    substring, not the label, so adding/removing a tracker doesn't
    invalidate any existing key, orphan rows age out via TTL.
    """
    cleaned = validate_tracked_authors(authors)

    from mimir.broker.writes import submit_write

    def _fn(conn):
        row = conn.execute(select(Inbox).where(Inbox.name == name)).first()
        if row is None:
            raise InboxNotFound(f"no inbox named {name!r}")
        conn.execute(
            update(Inbox).where(Inbox.name == name).values(tracked_authors=cleaned)
        )
        # Re-fetch to get the post-update state. The row mapping
        # includes all columns so we can construct a detached instance.
        updated = conn.execute(select(Inbox).where(Inbox.name == name)).one()
        return Inbox(**dict(updated._mapping))

    return submit_write(f"inbox:set_tracked_authors:{name}", _fn, timeout=30)
