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
"""
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from mimir.extensions import SessionLocal
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
) -> None:
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
    `examined=k` exactly across `k <= count`."""
    examined_total = 0

    with SessionLocal() as session:
        cursor: int | None = None
        while True:
            q = (
                select(Article)
                .order_by(Article.id.desc())
                .limit(batch_size)
            )
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
                break
            for article in batch:
                cursor = article.id
                examined_total += 1
                if limit is not None and examined_total > limit:
                    break
                result.examined += 1
                bucket = process_one(session, article, reprocess)
                setattr(result, bucket, getattr(result, bucket) + 1)
            session.commit()
            if progress is not None:
                progress(result)
            if limit is not None and examined_total > limit:
                break
