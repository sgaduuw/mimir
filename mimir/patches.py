"""Patch-body inspection: extract the set of file paths a message's
diff content touches, and a backfill walker for the
`article_files` join table.

Operates on `parsed.body` (the decoded plain-text body, already
surrogate-scrubbed). Pure function; no DB or git dependencies. The
caller (`mimir.ingest` and the backfill CLI) is responsible for
turning the returned set into `ArticleFile` rows.

Strong signal only: we match `diff --git a/<old> b/<new>` headers,
which are machine-generated and unambiguous. Prose-shaped mentions
("we should touch fs/foo/") are deliberately ignored — high-noise
and lousy precision. The cost is missing discussion-only threads in
the per-path reverse lookup; the win is that every match is a real
patch hunk.

The returned set carries the **`b/` path** — the post-rename
destination when the patch renames a file, the only path when it
doesn't. This matches what reviewers and `MAINTAINERS` globs expect:
the new location of the code. The `a/` path is recoverable from the
blob if a future surface ever needs it; ingest time is the wrong
moment to make that choice durable.
"""
import logging
import re
from typing import Callable

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleFile, Inbox
from mimir.store import MessageNotFound, read_message

logger = logging.getLogger(__name__)

# `diff --git a/<old> b/<new>` is git's machine-generated header.
# The paths are quoted only when they contain shell-special bytes
# (rare); in the common case they're literal. We allow any non-
# whitespace run for the path bodies and validate via the leading
# `a/` / `b/` prefixes.
#
# Why anchored with `^` + MULTILINE rather than searching anywhere:
# quoted bodies sometimes carry `diff --git` lines from earlier in
# a thread (a reviewer pasting a hunk). Anchoring at line-start in
# the body's own context catches the patch author's diffs and
# misses re-quoted snippets — which is the right precision trade
# (a reviewer's quoted diff doesn't represent "files this patch
# touches").
#
# Defensive shape: the `a/` and `b/` paths are captured separately
# so future surfaces (rename trails, file-history scrubbing) can
# read both; we currently only emit the `b/` side.
_DIFF_GIT_RE = re.compile(
    r"^diff --git a/(?P<old>\S+) b/(?P<new>\S+)\s*$",
    re.MULTILINE,
)


def extract_touched_paths(body: str | None) -> set[str]:
    """Return the set of post-rename paths the patch body touches.

    Returns an empty set on:
    - non-patch bodies (no `diff --git` headers)
    - None / empty input
    - bodies where every `diff --git` line is inside a quoted block
      (the regex anchors at column 0; quoted lines start with `>`
      and don't match)

    Quoted-block detection is intentionally simplistic — we let the
    line-start anchor do the work. A reviewer who unquotes a hunk
    when responding (rare; git-send-email doesn't quote diffs the
    way mailers quote prose) will have those lines treated as fresh
    diffs. Acceptable: that's effectively a reply containing a
    patch hunk, which IS a fresh diff for index purposes."""
    if not body:
        return set()
    return {m.group("new") for m in _DIFF_GIT_RE.finditer(body)}


# Page size for the backfill walker. ~6M articles on lkml-scale →
# 6000 batches at 1000 each; small enough that a Ctrl-C loses ≤1k
# articles of work but big enough that the per-batch overhead
# (transaction, query planning) amortises.
_BACKFILL_BATCH = 1000


class BackfillResult(BaseModel):
    """Outcome counters for `backfill_article_files`. Every article
    examined lands in exactly one bucket. `skipped` covers articles
    that already had ArticleFile rows (idempotent re-runs) plus
    articles whose mirror is unreachable from this host."""
    examined: int = 0
    indexed: int = 0   # had a patch body and one or more paths landed
    no_diff: int = 0   # body parsed but no `diff --git` headers
    skipped: int = 0   # already had rows, or unreadable
    failed: int = 0    # parse error reading the blob


def backfill_article_files(
    limit: int | None = None,
    reprocess: bool = False,
    progress: Callable[["BackfillResult"], None] | None = None,
) -> BackfillResult:
    """Walk articles, extract diff-touched paths, insert ArticleFile
    rows. Idempotent: articles with existing rows are skipped unless
    `reprocess=True` (which deletes existing rows before re-extracting
    — useful after an extractor change).

    Newest-first ordering so a `--limit`-bounded session covers the
    most-recently-active articles first; that's where the
    subsystem-header surface is most visible to a user."""
    result = BackfillResult()
    examined_total = 0

    with SessionLocal() as session:
        # ID-based pagination via `WHERE id < cursor` so we don't
        # re-scan rows we already finished. Newest = highest id, so
        # walk descending.
        cursor: int | None = None
        while True:
            q = (
                select(Article)
                .options(
                    selectinload(Article.lists),
                    # Prefer canonical_inbox when picking which
                    # mirror to re-read from (deterministic for
                    # cross-posts). joinedload because it's a
                    # nullable many-to-one — one JOIN, no extra
                    # query, no N+1 risk on the article loop.
                    joinedload(Article.canonical_inbox),
                )
                .order_by(Article.id.desc())
                .limit(_BACKFILL_BATCH)
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
                processed = _process_one(session, article, reprocess=reprocess)
                # `_process_one` returns one of: "indexed", "no_diff",
                # "skipped", "failed". Bump the matching counter.
                setattr(result, processed, getattr(result, processed) + 1)

            session.commit()
            if progress is not None:
                progress(result)
            if limit is not None and examined_total > limit:
                break

    return result


def _process_one(session, article: Article, reprocess: bool) -> str:
    """Handle one article. Returns the bucket name it lands in."""
    # Existing-rows check. `article.files` would also work but
    # selectinload didn't preload it; a COUNT is cheap and avoids
    # pulling the rows.
    has_rows = session.execute(
        select(ArticleFile.article_id)
        .where(ArticleFile.article_id == article.id)
        .limit(1)
    ).first() is not None
    if has_rows and not reprocess:
        return "skipped"
    if has_rows and reprocess:
        session.execute(
            ArticleFile.__table__.delete()
            .where(ArticleFile.__table__.c.article_id == article.id)
        )

    # Pick the canonical inbox to re-read the body. For cross-posts
    # this is the authoritative attribution; `article.lists[0]` is
    # ordering-dependent on the SQLA loader and was non-deterministic
    # for the same article across two backfills. canonical_inbox can
    # be NULL (warm-up period, or all observations fell below the
    # auto-promotion threshold), so fall back to the first lists
    # entry only then.
    inbox: Inbox | None = article.canonical_inbox
    if inbox is None:
        if not article.lists:
            return "skipped"
        inbox = session.get(Inbox, article.lists[0].inbox_id)
    if inbox is None:
        return "skipped"

    try:
        parsed = read_message(session, inbox, article.message_id)
    except (MessageNotFound, KeyError):
        # Mirror unreachable on this host, or the recorded SHA isn't
        # in the local repo (dulwich raises bare KeyError for that).
        # Common in dev and after a partial-mirror rebuild; defer
        # the work rather than fail loudly — a re-run from a host
        # with the full mirror picks the article up.
        return "skipped"
    except Exception as exc:
        logger.warning(
            "backfill: parse failure for article %d (%s): %r",
            article.id, article.message_id, exc,
        )
        return "failed"

    paths = extract_touched_paths(parsed.body)
    if not paths:
        return "no_diff"
    for path in sorted(paths):
        session.add(ArticleFile(article_id=article.id, path=path))
    return "indexed"


# Re-export for callers (CLI) so the import surface stays tight.
__all__ = [
    "BackfillResult",
    "backfill_article_files",
    "extract_touched_paths",
]
