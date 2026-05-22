"""Newest-first article-walker shared by the patch-metadata backfills.

`patches.backfill_article_files`, `trailers.backfill_article_trailers`,
and `patch_series.backfill_patch_series` all walk the same path: ID-
cursor pagination over `articles` newest-first, in batches of
`_BACKFILL_BATCH`, with the same `--limit` and `progress` semantics.
The only per-helper specifics are which `BackfillResult` shape is
returned, what `_process_one(session, article, reprocess)` does
inside the loop, and whether `Article.lists` needs preloading (only
the two body-re-reading helpers do).

Three callers across three modules clear the "second caller exists"
threshold for sharing; the alternative would let the walker shells
drift on every future change. Internal, not part of the public
import surface (underscore-prefixed).

`max_seconds` + `start_cursor` add cooperative scheduling for the
broker (Phase 2.2): a chunk runs for at most `max_seconds`,
checkpoints its progress by returning the last-processed `Article.id`
as the continuation cursor, and the broker handler returns that to
its CLI caller which resumes via a follow-up RPC. The full archive
is processed across N chunks rather than one multi-hour RPC, so
queued cache writes and other long ops can run between chunks.
Direct (non-broker) callers pass `max_seconds=None` and ignore the
return tuple, preserving the pre-2.2 shape.
"""

import time
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from mimir.extensions import SessionLocal, write_transaction
from mimir.models import Article


def walk_articles(
    result: Any,
    process_one: Callable[[Session, Article, bool], str],
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
    and bumping the matching bucket on `result`.

    `process_one(session, article, reprocess)` returns a string that
    matches a field name on `result` (e.g. `"indexed"`, `"skipped"`,
    `"failed"`). The walker bumps `result.examined` once per row, then
    `result.<bucket>` by the return value. Caller owns the
    `BackfillResult` shape; the walker doesn't care what fields exist
    so long as the return strings hit them.

    `preload_lists=True` eager-loads `Article.lists` via selectinload
    (needed by the body-re-reading helpers, patches and trailers
    so the inbox lookup doesn't N+1). `patch_series` reads only the
    `Article.subject` / `author` columns and sets `preload_lists=False`
    to save the join.

    `progress(result)` fires once per batch boundary. `limit` caps
    `examined` (post-increment, pre-process), so `limit=k` produces
    `examined=k` exactly across `k <= count`.

    `label` is threaded through to `write_transaction()` for the
    slow-write WARNING; callers pass e.g. `"backfill_article_files"`
    so a long lock-hold is identifiable in the broker-vs-scheduler
    correlation log.

    `max_seconds`: when not None, time-bound this chunk. Deadline
    checked at batch boundaries (after commit) so partial progress
    is always persisted before returning. Returns
    `(partial=True, continuation=<last id processed>)` if the
    deadline elapses before the walk completes.

    `start_cursor`: when not None, resume from this Article.id. The
    walker reads `Article.id < start_cursor` on the first batch.
    Used by the broker to continue a chunked walk; pair with the
    `continuation` value the prior chunk returned.

    Return: `(partial, continuation)`. `partial=False, continuation=None`
    when the walk completed naturally (either ran out of articles or
    hit `limit`). `partial=True, continuation=N` when `max_seconds`
    elapsed; resume by passing `start_cursor=N` to the next call.
    """
    examined_total = 0
    last_processed: int | None = None
    deadline: float | None = (
        time.monotonic() + max_seconds if max_seconds is not None else None
    )

    # BEGIN IMMEDIATE per batch transaction. The walker reads the
    # next batch of articles, mutates them in process_one, then
    # commits; without IMMEDIATE the first commit-then-next-read
    # interleave can trip SQLITE_BUSY_SNAPSHOT against the web
    # tier's cache.set writes.
    with write_transaction(label), SessionLocal() as session:
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
            for article in batch:
                cursor = article.id
                examined_total += 1
                if limit is not None and examined_total > limit:
                    break
                result.examined += 1
                bucket = process_one(session, article, reprocess)
                setattr(result, bucket, getattr(result, bucket) + 1)
                last_processed = article.id
            session.commit()
            if progress is not None:
                progress(result)
            if limit is not None and examined_total > limit:
                return False, None
            if deadline is not None and time.monotonic() >= deadline:
                # Time budget elapsed. Everything we processed is
                # committed; report the last id we touched so the
                # follow-up chunk resumes at `Article.id < last_processed`.
                return True, last_processed
