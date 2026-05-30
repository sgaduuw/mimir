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

from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, func, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mimir.broker.writes import WriteFuture, WriteOp
from mimir.models import (
    Article,
    ArticleList,
    Inbox,
    InboxAddressObservation,
    IngestState,
    ParseFailure,
)


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


def _submit_ingest_batch(writer, pending: "_PendingWrites") -> WriteFuture:
    """Phase 3b of the two-pool restructure.

    Compose a WriteOp running the full per-batch unit in dependency
    order on the writer's Connection, then submit it. The six
    operations, in order:

    1. Articles INSERT ... RETURNING id. For each `_ArticleInsert`,
       execute the INSERT and capture the returned id into
       `article_ids` so `_ArticleListInsert.article_index` resolves
       correctly for FKs.
    2. ArticleList INSERT. For each `_ArticleListInsert`, use
       `article_ids[article_index]` if `article_index != -1`, or
       `existing_article_id` otherwise (cross-post case: article
       already exists in the DB).
    3. ParseFailure DELETEs (recoveries) and UPSERTs (new failures).
       Records with `delete=True` delete the row at the PK;
       records with `delete=False` upsert with bumped attempts +
       refreshed error fields + updated last_attempt timestamp.
    4. InboxAddressObservation upsert batch. Mirrors `_flush_observations`
       from `mimir/ingest/epoch.py`: counts are additive; `last_seen`
       is the scalar MAX of existing and incoming.
    5. Inbox.last_article_date conditional UPDATE. Only runs when
       `pending.last_article_date_candidate` is not None; updates
       only when the candidate is greater than the current value
       (NULL or an earlier timestamp), so the column stays
       monotonically non-decreasing.
    6. IngestState.last_commit_sha UPSERT. This is the FINAL
       statement of the closure (cursor-as-final invariant): a
       crash or exception between statements rolls the whole batch
       back atomically; across batches the next tick re-walks from
       the last committed cursor and on_conflict_do_nothing /
       UNIQUE on articles.message_id makes the replay idempotent.

    Empty batches (nothing to write, no cursor advance) return a
    pre-resolved Future without submitting to the writer, mirroring
    `_submit_mainline_batch`'s early-return shape.
    """
    has_work = (
        pending.articles
        or pending.article_lists
        or pending.parse_failures
        or pending.address_observations
        or pending.last_article_date_candidate is not None
        or pending.last_commit_sha is not None
    )
    if not has_work:
        f: Future = Future()
        f.set_result(None)
        return f

    # Capture values up front so the closure is self-contained and
    # crosses the thread boundary without holding a reference to the
    # (potentially mutated) pending object.
    inbox_id = pending.inbox_id
    epoch = pending.epoch
    articles = list(pending.articles)
    article_lists = list(pending.article_lists)
    parse_failures = list(pending.parse_failures)
    address_observations = dict(pending.address_observations)
    last_article_date_candidate = pending.last_article_date_candidate
    last_commit_sha = pending.last_commit_sha

    def _fn(conn):
        # Step 1: INSERT articles, collect returned ids in order.
        article_ids: list[int] = []
        for art in articles:
            row = conn.execute(
                sqlite_insert(Article)
                .values(
                    message_id=art.message_id,
                    subject=art.subject,
                    author=art.author,
                    date=art.date,
                    thread_parent=art.thread_parent,
                    subject_normalized=art.subject_normalized,
                    canonical_inbox_id=art.canonical_inbox_id,
                    patch_series_key=art.patch_series_key,
                    patch_series_version=art.patch_series_version,
                    patch_series_position=art.patch_series_position,
                )
                .on_conflict_do_nothing(index_elements=["message_id"])
                .returning(Article.id)
            ).fetchone()
            # fetchone() returns None when on_conflict_do_nothing
            # silently suppressed the insert (duplicate message_id).
            # In that scenario the article already exists; append None
            # so the index remains aligned with the articles list.
            # _ArticleListInsert rows that reference this index via
            # article_index should use existing_article_id instead.
            article_ids.append(row[0] if row is not None else None)

        # Step 2: INSERT article_lists rows, resolving FKs by index.
        for al in article_lists:
            if al.article_index == -1:
                article_id = al.existing_article_id
            else:
                article_id = article_ids[al.article_index]
            if article_id is None:
                # The article INSERT was a no-op (duplicate); skip
                # the article_list row to avoid a NULL FK violation.
                continue
            conn.execute(
                sqlite_insert(ArticleList)
                .values(
                    article_id=article_id,
                    inbox_id=al.inbox_id,
                    epoch=al.epoch,
                    commit_sha=al.commit_sha,
                )
                .on_conflict_do_nothing(index_elements=["article_id", "inbox_id"])
            )

        # Step 3: ParseFailure DELETEs and UPSERTs.
        now = datetime.now(timezone.utc)
        for pf in parse_failures:
            if pf.delete:
                conn.execute(
                    delete(ParseFailure).where(
                        ParseFailure.inbox_id == pf.inbox_id,
                        ParseFailure.epoch == pf.epoch,
                        ParseFailure.commit_sha == pf.commit_sha,
                    )
                )
            else:
                if pf.already_recorded:
                    # Row already exists: bump attempts + refresh
                    # error fields + update last_attempt timestamp.
                    conn.execute(
                        sqlite_insert(ParseFailure)
                        .values(
                            inbox_id=pf.inbox_id,
                            epoch=pf.epoch,
                            commit_sha=pf.commit_sha,
                            error_class=pf.error_class or "",
                            error_message=pf.error_message or "",
                            first_seen=now,
                            last_attempt=now,
                            attempts=1,
                        )
                        .on_conflict_do_update(
                            index_elements=["inbox_id", "epoch", "commit_sha"],
                            set_={
                                "error_class": pf.error_class or "",
                                "error_message": pf.error_message or "",
                                "last_attempt": now,
                                "attempts": ParseFailure.attempts + 1,
                            },
                        )
                    )
                else:
                    conn.execute(
                        sqlite_insert(ParseFailure)
                        .values(
                            inbox_id=pf.inbox_id,
                            epoch=pf.epoch,
                            commit_sha=pf.commit_sha,
                            error_class=pf.error_class or "",
                            error_message=pf.error_message or "",
                            first_seen=now,
                            last_attempt=now,
                            attempts=1,
                        )
                        .on_conflict_do_nothing(
                            index_elements=["inbox_id", "epoch", "commit_sha"]
                        )
                    )

        # Step 4: InboxAddressObservation upsert batch. Mirrors
        # `_flush_observations` in epoch.py: counts are additive,
        # last_seen takes the scalar max of existing and incoming.
        if address_observations:
            rows = [
                {
                    "inbox_id": inbox_id,
                    "address": addr,
                    "count": count,
                    "last_seen": last_seen,
                }
                for addr, (count, last_seen) in address_observations.items()
            ]
            obs_stmt = sqlite_insert(InboxAddressObservation).values(rows)
            obs_stmt = obs_stmt.on_conflict_do_update(
                index_elements=["inbox_id", "address"],
                set_={
                    "count": InboxAddressObservation.count + obs_stmt.excluded.count,
                    # SQLite's scalar max(a, b): keeps the freshest
                    # timestamp when a stale tail batch lands after a
                    # newer one.
                    "last_seen": func.max(
                        InboxAddressObservation.last_seen,
                        obs_stmt.excluded.last_seen,
                    ),
                },
            )
            conn.execute(obs_stmt)

        # Step 5: Inbox.last_article_date conditional UPDATE. The WHERE
        # clause ensures the column is monotonically non-decreasing: we
        # only write when the candidate is strictly greater than the
        # current value (or the column is NULL, i.e. first ingest).
        if last_article_date_candidate is not None:
            conn.execute(
                update(Inbox)
                .where(
                    Inbox.id == inbox_id,
                    (Inbox.last_article_date.is_(None))
                    | (Inbox.last_article_date < last_article_date_candidate),
                )
                .values(last_article_date=last_article_date_candidate)
            )

        # Step 6 (FINAL): IngestState.last_commit_sha UPSERT. Must be
        # the last statement in this closure so a crash between earlier
        # statements rolls the batch back without advancing the cursor.
        # The next tick re-walks from the old cursor position; UNIQUE on
        # articles.message_id + on_conflict_do_nothing on article_lists
        # makes the replay idempotent.
        if last_commit_sha is not None:
            conn.execute(
                sqlite_insert(IngestState)
                .values(inbox_id=inbox_id, epoch=epoch, last_commit_sha=last_commit_sha)
                .on_conflict_do_update(
                    index_elements=["inbox_id", "epoch"],
                    set_={"last_commit_sha": last_commit_sha},
                )
            )

    return writer.submit(
        WriteOp(label=f"ingest:{epoch}:batch", fn=_fn),
    )
