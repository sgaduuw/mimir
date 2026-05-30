"""Patch-body inspection: extract the set of file paths a message's
diff content touches, and a backfill walker for the
`article_files` join table.

Operates on `parsed.body` (the decoded plain-text body, already
surrogate-scrubbed). Pure function; no DB or git dependencies. The
caller (`mimir.ingest` and the backfill CLI) is responsible for
turning the returned set into `ArticleFile` rows.

Strong signal only: we match `diff --git a/<old> b/<new>` headers,
which are machine-generated and unambiguous. Prose-shaped mentions
("we should touch fs/foo/") are deliberately ignored, high-noise
and lousy precision. The cost is missing discussion-only threads in
the per-path reverse lookup; the win is that every match is a real
patch hunk.

The returned set carries the **`b/` path**, the post-rename
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

from mimir._backfill import walk_articles
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
# misses re-quoted snippets, which is the right precision trade
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


# Hard ceiling on the number of distinct paths one patch body can
# contribute to `article_files`. Real-world series rarely touch more
# than ~50 files in one mail; 1000 is several orders of magnitude
# above any non-pathological patch. The cap defends against a
# crafted body with synthetic `diff --git` headers that would
# otherwise flood `article_files` with bogus rows and degrade the
# per-subsystem fan-outs that join on `(path, article_id)`.
MAX_TOUCHED_PATHS = 1000


def extract_touched_paths(body: str | None) -> set[str]:
    """Return the set of post-rename paths the patch body touches.

    Returns an empty set on:
    - non-patch bodies (no `diff --git` headers)
    - None / empty input
    - bodies where every `diff --git` line is inside a quoted block
      (the regex anchors at column 0; quoted lines start with `>`
      and don't match)

    Quoted-block detection is intentionally simplistic, we let the
    line-start anchor do the work. A reviewer who unquotes a hunk
    when responding (rare; git-send-email doesn't quote diffs the
    way mailers quote prose) will have those lines treated as fresh
    diffs. Acceptable: that's effectively a reply containing a
    patch hunk, which IS a fresh diff for index purposes.

    Caps the returned set at `MAX_TOUCHED_PATHS` entries. Real-world
    series stay well under that ceiling; the cap exists to bound
    work from a hostile or malformed message."""
    if not body:
        return set()
    out: set[str] = set()
    for m in _DIFF_GIT_RE.finditer(body):
        out.add(m.group("new"))
        if len(out) >= MAX_TOUCHED_PATHS:
            break
    return out


class BackfillResult(BaseModel):
    """Outcome counters for `backfill_article_files`. Every article
    examined lands in exactly one bucket. `skipped` covers articles
    that already had ArticleFile rows (idempotent re-runs) plus
    articles whose mirror is unreachable from this host.

    `partial` + `continuation` carry the cooperative-scheduling
    handoff between broker chunks (Phase 2.2). Direct (non-broker)
    callers always see `partial=False, continuation=None`.
    """

    examined: int = 0
    indexed: int = 0  # had a patch body and one or more paths landed
    no_diff: int = 0  # body parsed but no `diff --git` headers
    skipped: int = 0  # already had rows, or unreadable
    failed: int = 0  # parse error reading the blob
    partial: bool = False
    continuation: int | None = None

    def merge(self, other: "BackfillResult") -> "BackfillResult":
        """Sum counters with `other`, carrying `other`'s
        `partial`/`continuation` forward. Used by the CLI loop that
        aggregates per-chunk results from the broker."""
        return BackfillResult(
            examined=self.examined + other.examined,
            indexed=self.indexed + other.indexed,
            no_diff=self.no_diff + other.no_diff,
            skipped=self.skipped + other.skipped,
            failed=self.failed + other.failed,
            partial=other.partial,
            continuation=other.continuation,
        )


def backfill_article_files(
    limit: int | None = None,
    reprocess: bool = False,
    progress: Callable[["BackfillResult"], None] | None = None,
    *,
    max_seconds: float | None = None,
    start_cursor: int | None = None,
) -> BackfillResult:
    """Walk articles, extract diff-touched paths, insert ArticleFile
     rows. Idempotent: articles with existing rows are skipped unless
     `reprocess=True` (which deletes existing rows before re-extracting
    , useful after an extractor change).

     Newest-first ordering so a `--limit`-bounded session covers the
     most-recently-active articles first; that's where the
     subsystem-header surface is most visible to a user.

     `max_seconds` + `start_cursor` enable broker-side cooperative
     scheduling (Phase 2.2): the broker handler runs at most one
     chunk per RPC, then returns `partial=True, continuation=<last id>`
     so the CLI loops with a follow-up RPC. Direct callers leave
     both None and get the original full-walk behaviour."""
    from mimir._pending_backfill import _submit_article_files_batch

    result = BackfillResult()
    partial, continuation = walk_articles(
        result,
        _process_one,
        _submit_article_files_batch,
        limit=limit,
        reprocess=reprocess,
        progress=progress,
        label="backfill_article_files",
        max_seconds=max_seconds,
        start_cursor=start_cursor,
    )
    result.partial = partial
    result.continuation = continuation
    return result


def _process_one(session, article: Article, reprocess: bool) -> tuple[str, object]:
    """Per-article work for `backfill_article_files`. Returns
    `(bucket, pending_payload)` where pending_payload is an
    `_ArticleFilesPending` or None for buckets that produce no writes."""
    from mimir._pending_backfill import _ArticleFilesPending

    # Existing-rows check. `article.files` would also work but
    # selectinload didn't preload it; a COUNT is cheap and avoids
    # pulling the rows.
    has_rows = (
        session.execute(
            select(ArticleFile.article_id)
            .where(ArticleFile.article_id == article.id)
            .limit(1)
        ).first()
        is not None
    )
    if has_rows and not reprocess:
        return "skipped", None

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
            return "skipped", None
        inbox = session.get(Inbox, article.lists[0].inbox_id)
    if inbox is None:
        return "skipped", None

    try:
        parsed = read_message(session, inbox, article.message_id)
    except MessageNotFound, KeyError:
        # Mirror unreachable on this host, or the recorded SHA isn't
        # in the local repo (dulwich raises bare KeyError for that).
        # Common in dev and after a partial-mirror rebuild; defer
        # the work rather than fail loudly, a re-run from a host
        # with the full mirror picks the article up.
        return "skipped", None
    except Exception as exc:
        logger.warning(
            "backfill: parse failure for article %d (%s): %r",
            article.id,
            article.message_id,
            exc,
        )
        return "failed", None

    paths = extract_touched_paths(parsed.body)
    if not paths:
        return "no_diff", None

    return "indexed", _ArticleFilesPending(
        article_id=article.id,
        delete_first=(has_rows and reprocess),
        paths=sorted(paths),
    )


# Re-export for callers (CLI) so the import surface stays tight.
__all__ = [
    "BackfillResult",
    "backfill_article_files",
    "extract_touched_paths",
]
