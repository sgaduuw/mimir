"""Phase 3b: per-batch pending-writes carrier for ingest.

`ingest_epoch` builds up a `_PendingWrites` snapshot per message during
its read/compute phase (running on a query_only session from the
active ReadSessionPool), and at every batch boundary submits the
snapshot as one composite WriteOp via `_submit_ingest_batch`. Pure
data with no SQLAlchemy ORM objects so the snapshot crosses the
read-pool to writer-thread boundary cleanly without session affinity.

Underscore-prefixed module: internal to `mimir.ingest`; not part
of the public surface.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class _ArticleInsert:
    """One row to insert into `articles`. id is None pre-INSERT; the
    composite WriteOp fills it from the RETURNING clause and the matching
    `_ArticleListInsert` rows pick it up by index."""

    message_id: str
    subject: str | None
    author: str | None
    date: datetime | None
    thread_parent: str | None
    subject_normalized: str
    canonical_inbox_id: int | None
    patch_series_key: str | None = None
    patch_series_version: str | None = None
    patch_series_position: int | None = None


@dataclass
class _ArticleListInsert:
    """One row to insert into `article_lists`. `article_index` points at
    the corresponding `_ArticleInsert` in this batch (or -1 if the
    Article already existed in the DB, in which case `existing_article_id`
    carries the FK value)."""

    article_index: int  # index into _PendingWrites.articles; -1 for existing
    existing_article_id: int | None
    inbox_id: int
    epoch: str
    commit_sha: str


@dataclass
class _ParseFailureRecord:
    """ParseFailure upsert (new failure) or DELETE (recovered). `delete`
    True means delete the row at the given key; False means upsert with
    the carried metadata."""

    inbox_id: int
    epoch: str
    commit_sha: str
    delete: bool
    error_class: str | None = None
    error_message: str | None = None
    already_recorded: bool = False


@dataclass
class _PendingWrites:
    """Snapshot of one batch's pending writes. Built up on the read /
    compute phase; consumed by `_submit_ingest_batch` at the flush
    boundary. Carries no SQLAlchemy ORM objects, only plain data, so it
    crosses the read-pool / writer-thread boundary cleanly (no session
    affinity)."""

    inbox_id: int
    epoch: str
    articles: list[_ArticleInsert] = field(default_factory=list)
    article_lists: list[_ArticleListInsert] = field(default_factory=list)
    parse_failures: list[_ParseFailureRecord] = field(default_factory=list)
    address_observations: dict[str, tuple[int, datetime]] = field(default_factory=dict)
    last_article_date_candidate: datetime | None = None
    # Cursor advance, the FINAL field written by the composite WriteOp.
    last_commit_sha: str | None = None
