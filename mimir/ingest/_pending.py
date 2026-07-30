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

from sqlalchemy import delete, func, insert as sa_insert, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mimir.broker.writes import WriteFuture, WriteOp
from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    ArticleTrailer,
    Inbox,
    InboxAddressObservation,
    IngestState,
    ParseFailure,
)


@dataclass
class _ArticleInsert:
    """One row to insert into `articles`. id is None pre-INSERT; the
    composite WriteOp fills it from the RETURNING clause and the matching
    `_ArticleListInsert` rows pick it up by index.

    `touched_paths` carries the `diff --git b/<path>` paths from
    `extract_touched_paths`; each becomes one `ArticleFile` row keyed on
    the returned `article_id`. Empty list for non-patch articles.

    `trailer_rows` carries `(role, name, address)` tuples from
    `extract_trailers`; each becomes one `ArticleTrailer` row keyed on
    the returned `article_id`. Empty list for articles with no review
    attestation trailers. `address_normalized` is derived in the WriteOp
    closure (lowercase of `address`) rather than carried here to keep the
    dataclass lean."""

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
    touched_paths: list[str] = field(default_factory=list)
    trailer_rows: list[tuple[str, str, str]] = field(default_factory=list)


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


def _set_subtree_root(conn, inbox_id: int, article_id: int, root_id) -> None:
    """Set `thread_root_id` for everything hanging off `article_id` in
    this inbox. `root_id=None` invalidates the subtree so the backfill
    recomputes it.

    A cyclic `thread_parent` (sender-controlled, unguarded at ingest)
    cannot spin this: `thread_parent` is single-valued, so any cycle
    reachable from this article must contain it, and the
    `a.id != :aid` guard cuts the walk there.
    """
    conn.execute(
        text(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT a.id
                  FROM articles a
                  JOIN article_lists al ON al.article_id = a.id
                  JOIN articles self ON self.id = :aid
                 WHERE al.inbox_id = :ix
                   AND a.thread_parent = self.message_id
                   AND a.id != :aid
                UNION
                SELECT a.id
                  FROM articles a
                  JOIN article_lists al ON al.article_id = a.id
                  JOIN articles p ON p.message_id = a.thread_parent
                  JOIN descendants d ON d.id = p.id
                 WHERE al.inbox_id = :ix
                   AND a.id != :aid
            )
            UPDATE article_lists
               SET thread_root_id = :root
             WHERE inbox_id = :ix
               AND article_id IN (SELECT id FROM descendants)
            """
        ),
        {"aid": article_id, "ix": inbox_id, "root": root_id},
    )


def _resolve_thread_root(conn, inbox_id: int, article_id: int, parent_msgid) -> None:
    """Set `article_lists.thread_root_id` for one freshly-inserted
    `(article, inbox)` row, then re-root anything that was waiting on it.

    Two halves, because messages do not arrive in thread order.

    **Inherit or self.** The row takes its parent's root IN THE SAME
    INBOX. A parent that is absent from this inbox (an off-list
    ancestor, or a cross-post whose root went to a different list)
    leaves this article its own root, which is exactly what
    `find_thread_root` concludes by walking up and finding nothing.

    **Re-root the waiters.** A child ingested before its parent, which
    happens across epoch boundaries, self-rooted on arrival. When the
    parent lands, that child AND its whole subtree have to move onto
    the parent's root, or the conversation stays split in two, quietly,
    with both halves rendering fine. This is the case CONTEXT.md cites
    as the reason materialised roots were deferred.

    A cyclic `thread_parent` (sender-controlled, unguarded at ingest)
    cannot spin the subtree walk: `thread_parent` is single-valued, so
    any cycle reachable from this article must contain it, and the
    `a.id != :aid` guard cuts the walk there. (`UNION` rather than
    `UNION ALL` dedupes, but it is the guard that terminates, not the
    set semantics.) Under a cycle the
    members converge on whichever member was reached first; that is
    deliberately NOT what `find_thread_root` returns (it walks to
    MAX_DEPTH and lands wherever `1000 mod cycle_length` puts it), and
    the difference is documented in `tests/test_thread_roots.py`.
    """
    root_id = None
    parent_present = False
    if parent_msgid:
        row = conn.execute(
            text(
                "SELECT al.thread_root_id, al.article_id "
                "FROM article_lists al JOIN articles p ON p.id = al.article_id "
                "WHERE p.message_id = :mid AND al.inbox_id = :ix"
            ),
            {"mid": parent_msgid, "ix": inbox_id},
        ).fetchone()
        # A self-referential In-Reply-To resolves the parent to this
        # very row; treat it as having no parent rather than pointing
        # the article at its own unset root.
        if row is not None and row[1] != article_id:
            parent_present = True
            root_id = row[0]

    if parent_present and root_id is None:
        # The parent is here but has no root yet. Do NOT fall back to
        # the parent's article id: that is only right when the parent
        # happens to BE a root, and when it is not the row is wrong AND
        # unrepairable, because `seed_roots` and `propagate` both key on
        # `IS NULL` and skip anything non-NULL forever.
        #
        # This is the state of every row between the migration landing
        # and the backfill finishing, which is the normal deploy window:
        # the broker migrates at startup, `mimir-tasks` begins firing
        # `update` immediately, and the backfill is a manual operator
        # step that may be hours later.
        #
        # Leave this row NULL, AND invalidate anything already hanging
        # off it. A descendant that self-rooted earlier (its parent was
        # absent at the time) is now stale, and stale is worse than
        # unset: it is non-NULL, so `seed_roots` and `propagate` both
        # skip it forever and the backfill can never repair it. That is
        # the same permanent-corruption shape as inheriting the
        # parent's id, just displaced onto a sibling.
        #
        # NULL is always safe to write here: it means "not yet
        # computed", readers fall back to the recursive CTE, and the
        # backfill recomputes the whole subtree correctly. The cost is
        # recomputation, never a wrong answer.
        _set_subtree_root(conn, inbox_id, article_id, None)
        return
    if root_id is None:
        root_id = article_id

    conn.execute(
        text(
            "UPDATE article_lists SET thread_root_id = :root "
            "WHERE article_id = :aid AND inbox_id = :ix"
        ),
        {"root": root_id, "aid": article_id, "ix": inbox_id},
    )

    _set_subtree_root(conn, inbox_id, article_id, root_id)


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
    4. InboxAddressObservation upsert batch: counts are additive;
       `last_seen` is the scalar MAX of existing and incoming.
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
        # Step 1: INSERT articles, collect returned ids in order. For
        # each successfully inserted article, also insert its
        # ArticleFile rows (diff-touched paths) and ArticleTrailer rows
        # (review-attestation trailers) using the returned id as the FK.
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
            article_id = row[0] if row is not None else None
            article_ids.append(article_id)

            # ArticleFile rows: one per diff-touched path. Composite
            # (article_id, path) PK means repeated ingest of the same
            # message is idempotent (on_conflict_do_nothing).
            if article_id is not None and art.touched_paths:
                for path in art.touched_paths:
                    conn.execute(
                        sqlite_insert(ArticleFile)
                        .values(article_id=article_id, path=path)
                        .on_conflict_do_nothing(index_elements=["article_id", "path"])
                    )

            # ArticleTrailer rows: one per review-attestation trailer.
            # ArticleTrailer has an autoincrement `id` PK, not a
            # composite natural key; we only insert when the article was
            # freshly created (article_id not None means the INSERT did
            # not hit on_conflict_do_nothing), so there is no duplicate
            # risk: a second ingest of the same message lands a
            # duplicate Article via on_conflict_do_nothing (article_id
            # stays None) and we skip trailer insertion entirely.
            if article_id is not None and art.trailer_rows:
                for role, name, address in art.trailer_rows:
                    conn.execute(
                        sa_insert(ArticleTrailer).values(
                            article_id=article_id,
                            role=role,
                            name=name,
                            address=address,
                            address_normalized=address.lower(),
                        )
                    )

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
            # Maintain the materialised root for this (article, inbox)
            # pair, and re-root anything that was waiting on it.
            # The accumulator already carries the parent for rows this
            # batch inserted; only cross-post links (article_index -1,
            # article already in the DB) need the lookup.
            if al.article_index != -1:
                parent_msgid = articles[al.article_index].thread_parent
            else:
                parent_msgid = conn.execute(
                    text("SELECT thread_parent FROM articles WHERE id = :aid"),
                    {"aid": article_id},
                ).scalar()
            _resolve_thread_root(conn, al.inbox_id, article_id, parent_msgid)

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

        # Step 4: InboxAddressObservation upsert batch: counts are
        # additive, last_seen takes the scalar max of existing and incoming.
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


def _submit_promote_list_address(writer, inbox_id: int) -> WriteFuture:
    """Phase 3b of the two-pool restructure.

    Compose a WriteOp that promotes an inbox's `list_address`: read the
    observations tally, check for a clear modal winner that meets the
    count + dominance thresholds (MIN_PROMOTE_OBSERVATIONS /
    PROMOTE_DOMINANCE), and promote Inbox.list_address from NULL to that
    address. The gate boundaries are pinned by the promotion tests in
    tests/test_ingest/test_epoch.py.

    Returns the WriteFuture so callers can await .result() before the
    next operation. Sub-ms execution time in practice.
    """
    # Import the constants from epoch.py where they live.
    from mimir.ingest.epoch import MIN_PROMOTE_OBSERVATIONS, PROMOTE_DOMINANCE

    def _fn(conn):
        # Read the inbox row and observations to check the promotion
        # threshold. Use scalar select of list_address to check if
        # promotion is needed.
        list_address = conn.execute(
            select(Inbox.list_address).where(Inbox.id == inbox_id)
        ).scalar_one_or_none()
        if list_address is not None:
            return None

        rows = conn.execute(
            select(
                InboxAddressObservation.address,
                InboxAddressObservation.count,
            )
            .where(InboxAddressObservation.inbox_id == inbox_id)
            .order_by(InboxAddressObservation.count.desc())
            .limit(2)
        ).all()
        if not rows:
            return None

        top_addr, top_count = rows[0]
        if top_count < MIN_PROMOTE_OBSERVATIONS:
            return None

        second_count = rows[1][1] if len(rows) > 1 else 0
        if top_count / max(top_count + second_count, 1) < PROMOTE_DOMINANCE:
            return None

        conn.execute(
            update(Inbox).where(Inbox.id == inbox_id).values(list_address=top_addr)
        )

    return writer.submit(WriteOp(label=f"promote_list_address:{inbox_id}", fn=_fn))


def _submit_analyze(writer, inbox_name: str) -> WriteFuture:
    """Phase 3b of the two-pool restructure.

    Compose a WriteOp whose closure runs ANALYZE on the writer's
    connection. Replaces the auto-ANALYZE-after-ingest write_transaction
    block in ingest_inbox(). The label carries the inbox name for
    slow-write WARNING correlation (matching the legacy write_transaction
    label shape).

    Returns the WriteFuture so callers can await .result() before the
    next operation.
    """

    def _fn(conn):
        conn.execute(text("ANALYZE"))

    return writer.submit(WriteOp(label=f"auto_analyze:{inbox_name}", fn=_fn))
