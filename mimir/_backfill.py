"""Newest-first article-walker shared by the patch-metadata backfills.

`patches.backfill_article_files`, `trailers.backfill_article_trailers`,
and `patch_series.backfill_patch_series` all walk the same path: ID-
cursor pagination over `articles` newest-first, in batches of
`batch_size`, with the same `--limit` and `progress` semantics.
The per-helper specifics are which `BackfillResult` shape is returned,
what `_process_one(session, article, reprocess)` returns (a (bucket,
pending_payload) tuple where pending_payload is consumed by the caller-
supplied `flush_batch(writer, payloads)` helper), and whether
`Article.lists` needs preloading (only the two body-re-reading
helpers do).

Phase 3c of the broker two-pool restructure: reads run on a
query_only session from `_context.get_active_pool()`; writes go
through the active `WriterThread` via the per-backfill `flush_batch`
callable. The pre-3c `write_transaction(label) + SessionLocal()`
block is gone; per-batch composite WriteOps replace the
single-transaction-per-walk locking model.
"""

import time
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from mimir.models import Article


def walk_articles(
    result: Any,
    process_one: Callable[[Session, Article, bool], tuple[str, Any]],
    flush_batch: Callable[[Any, list[Any]], Any],
    *,
    limit: int | None = None,
    reprocess: bool = False,
    progress: Callable[[Any], None] | None = None,
    preload_lists: bool = True,
    batch_size: int = 1000,
    label: str = "walk_articles",
    max_seconds: float | None = None,
    start_cursor: int | None = None,
) -> tuple[bool, int | None]:
    """Walk every Article newest-first, calling `process_one` per row
    and dispatching one composite WriteOp per batch via `flush_batch`.

    `process_one(session, article, reprocess)` returns
    `(bucket_name, pending_payload)`. The walker bumps `result.examined`
    once per row, then `result.<bucket_name>` by one. Pending payloads
    accumulate per batch; payloads of `None` are skipped (used by
    backfills for buckets that produce no write).

    `flush_batch(writer, payloads)` returns a `WriteFuture`. The walker
    awaits `.result()` before composing the next batch and before
    checking the time budget.

    `preload_lists=True` eager-loads `Article.lists` via selectinload
    (needed by the body-re-reading helpers, `patches` and `trailers`);
    `patch_series` reads only `Article.subject` / `author` /
    `thread_parent` and sets `preload_lists=False` to save the join.

    `progress(result)` fires once per batch boundary, after the
    WriteOp commits.

    `label` is preserved for slow-write WARNING correlation in
    callers (the writer thread does its own slow-write logging on
    the composite WriteOp's wall time).

    `max_seconds`: when not None, time-bound this chunk. Deadline
    checked at batch boundaries (after `flush_batch(...).result()`)
    so partial progress is always persisted before returning.
    Returns `(partial=True, continuation=<last id processed>)` if the
    deadline elapses before the walk completes.

    `start_cursor`: when not None, resume from `Article.id < start_cursor`.

    Phase 3c: reads run on a query_only session from
    `_context.get_active_pool()`; writes go through
    `_context.get_active_writer()` via `flush_batch`.
    """
    from mimir.broker._context import get_active_pool, get_active_writer

    pool = get_active_pool()
    writer = get_active_writer()

    examined_total = 0
    last_processed: int | None = None
    deadline: float | None = (
        time.monotonic() + max_seconds if max_seconds is not None else None
    )

    with pool.session() as session:
        cursor: int | None = start_cursor
        while True:
            q = select(Article).order_by(Article.id.desc()).limit(batch_size)
            if preload_lists:
                q = q.options(
                    selectinload(Article.lists),
                    # Prefer canonical_inbox when picking which mirror
                    # to re-read the body from. joinedload because it's
                    # a nullable many-to-one, one JOIN with no N+1 on
                    # the article loop.
                    joinedload(Article.canonical_inbox),
                )
            if cursor is not None:
                q = q.where(Article.id < cursor)
            batch = list(session.execute(q).scalars())
            if not batch:
                return False, None

            batch_payloads: list[Any] = []
            limit_reached = False
            for article in batch:
                cursor = article.id
                examined_total += 1
                if limit is not None and examined_total > limit:
                    limit_reached = True
                    break
                result.examined += 1
                bucket, payload = process_one(session, article, reprocess)
                setattr(result, bucket, getattr(result, bucket) + 1)
                if payload is not None:
                    batch_payloads.append(payload)
                last_processed = article.id

            flush_batch(writer, batch_payloads).result(timeout=120)
            # Release the read snapshot so the next batch's SELECT sees
            # the writes that just committed via the writer thread.
            session.commit()

            if progress is not None:
                progress(result)
            if limit_reached:
                return False, None
            if deadline is not None and time.monotonic() >= deadline:
                # Time budget elapsed. Everything we processed is
                # committed; report the last id we touched so the
                # follow-up chunk resumes at `Article.id < last_processed`.
                return True, last_processed
