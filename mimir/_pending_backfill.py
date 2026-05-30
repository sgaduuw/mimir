"""Per-batch pending-writes carriers for the four patch-metadata
backfills (Phase 3c of the broker two-pool restructure).

Each backfill's `process_one` returns a typed pending payload for the
current article; the walker accumulates payloads per batch; the
matching `_submit_*_batch` helper composes a single composite WriteOp
covering the accumulated payloads and submits it to the active
WriterThread. The closure consumes raw SQLAlchemy Core (not an ORM
session) because the writer thread has no ORM session lifecycle to
share with the read pool.

Underscore-prefixed; not part of the public ingest / patches /
trailers / patch_series surfaces.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class _ArticleFilesPending:
    """Per-article pending writes for `backfill_article_files`.

    `delete_first=True` instructs the WriteOp closure to DELETE existing
    `ArticleFile` rows for this article_id before INSERTing the new
    `paths`. Used by `--reprocess`.
    """

    article_id: int
    delete_first: bool = False
    paths: list[str] = field(default_factory=list)


@dataclass
class _ArticleTrailerInsert:
    """One ArticleTrailer row. Mirrors the shape `mimir.trailers.extract_trailers`
    yields, with `address_normalized` derived in the WriteOp closure
    (lowercase of `address`) rather than carried here."""

    role: str
    name: str
    address: str


@dataclass
class _ArticleTrailersPending:
    """Per-article pending writes for `backfill_article_trailers`.

    `delete_first=True` instructs the WriteOp closure to DELETE existing
    `ArticleTrailer` rows for this article_id before INSERTing the new
    `trailers`. Used by `--reprocess`.
    """

    article_id: int
    delete_first: bool = False
    trailers: list[_ArticleTrailerInsert] = field(default_factory=list)


@dataclass
class _PatchSeriesPending:
    """Per-article pending writes for `backfill_patch_series`.

    All three columns are nullable; the WriteOp closure issues
    `UPDATE articles SET patch_series_key=?, patch_series_version=?,
    patch_series_position=? WHERE id=?` with the carried values. The
    UPDATE always fires (even for the "skipped"/"not_cover" buckets
    where the values may match what's already in the DB), keeping
    the closure trivially uniform.
    """

    article_id: int
    patch_series_key: str | None
    patch_series_version: str | None
    patch_series_position: int | None


@dataclass
class _CanonicalPending:
    """Per-article pending writes for `backfill_canonicals`.

    `new_canonical_inbox_id` is the resolved canonical-inbox id. The
    closure issues `UPDATE articles SET canonical_inbox_id=? WHERE id=?
    AND (canonical_inbox_id IS DISTINCT FROM ?)` so a no-op resolution
    doesn't churn rows.

    `observation_deltas` is `{inbox_id: {address: (count, last_seen)}}`;
    the closure upserts every (inbox_id, address) pair via
    `INSERT ... ON CONFLICT DO UPDATE SET count = count + excluded.count,
    last_seen = MAX(last_seen, excluded.last_seen)`.
    """

    article_id: int
    new_canonical_inbox_id: int | None
    observation_deltas: dict[int, dict[str, tuple[int, datetime]]] = field(
        default_factory=dict
    )
