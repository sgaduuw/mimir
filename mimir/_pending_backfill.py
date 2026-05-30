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

from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, insert as sa_insert, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mimir.broker.writes import WriteFuture, WriteOp
from mimir.models import Article, ArticleFile, ArticleTrailer


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


def _submit_article_files_batch(
    writer, payloads: list[_ArticleFilesPending]
) -> WriteFuture:
    """Compose the per-batch composite WriteOp for `backfill_article_files`.

    The closure iterates `payloads` in order. For each:
    - if `delete_first`, issue `DELETE FROM article_files WHERE article_id = ?`.
    - then issue `INSERT INTO article_files (article_id, path) VALUES (?, ?)
      ON CONFLICT (article_id, path) DO NOTHING` for each path.

    Empty payload list returns a pre-resolved future without queueing.
    """
    if not payloads:
        f: Future = Future()
        f.set_result(None)
        return f

    def _fn(conn):
        for p in payloads:
            if p.delete_first:
                conn.execute(
                    delete(ArticleFile).where(ArticleFile.article_id == p.article_id)
                )
            for path in p.paths:
                conn.execute(
                    sqlite_insert(ArticleFile)
                    .values(article_id=p.article_id, path=path)
                    .on_conflict_do_nothing(index_elements=["article_id", "path"])
                )

    return writer.submit(WriteOp(label="backfill:article_files:batch", fn=_fn))


def _submit_article_trailers_batch(
    writer, payloads: list[_ArticleTrailersPending]
) -> WriteFuture:
    """Compose the per-batch composite WriteOp for
    `backfill_article_trailers`.

    Mirror of `_submit_article_files_batch`: per-payload DELETE if
    `delete_first`, then INSERT per trailer. `address_normalized` is
    derived in the closure as `address.lower()`. ArticleTrailer has an
    autoincrement `id` PK with no natural UNIQUE on
    (article_id, address, role), so the INSERT is a plain VALUES.
    """
    if not payloads:
        f: Future = Future()
        f.set_result(None)
        return f

    def _fn(conn):
        for p in payloads:
            if p.delete_first:
                conn.execute(
                    delete(ArticleTrailer).where(
                        ArticleTrailer.article_id == p.article_id
                    )
                )
            for t in p.trailers:
                conn.execute(
                    sa_insert(ArticleTrailer).values(
                        article_id=p.article_id,
                        role=t.role,
                        name=t.name,
                        address=t.address,
                        address_normalized=t.address.lower(),
                    )
                )

    return writer.submit(WriteOp(label="backfill:article_trailers:batch", fn=_fn))


def _submit_patch_series_batch(
    writer, payloads: list[_PatchSeriesPending]
) -> WriteFuture:
    """Compose the per-batch composite WriteOp for `backfill_patch_series`.

    The closure issues N `UPDATE articles SET patch_series_key=?,
    patch_series_version=?, patch_series_position=? WHERE id=?` in
    payload order (the caller is responsible for sorting by
    `article_id` ascending when ordering matters; SQLite's writer
    lock makes intra-broker races impossible, so order is purely
    a defensive consideration). NULL values overwrite previously-set
    values (used by the orphan-clear / not-a-cover paths).
    """
    if not payloads:
        f: Future = Future()
        f.set_result(None)
        return f

    def _fn(conn):
        for p in payloads:
            conn.execute(
                update(Article)
                .where(Article.id == p.article_id)
                .values(
                    patch_series_key=p.patch_series_key,
                    patch_series_version=p.patch_series_version,
                    patch_series_position=p.patch_series_position,
                )
            )

    return writer.submit(WriteOp(label="backfill:patch_series:batch", fn=_fn))
