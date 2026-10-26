"""Patch-body inspection: extract the file paths a message's diff
content touches, with the lines it adds and removes in each, and a
backfill walker for the `article_files` join table.

Operates on `parsed.body` (the decoded plain-text body, already
surrogate-scrubbed). Pure function; no DB or git dependencies. The
caller (`mimir.ingest` and the backfill CLI) is responsible for
turning the returned paths and counts into `ArticleFile` rows.

Strong signal only: we match `diff --git a/<old> b/<new>` headers,
which are machine-generated and unambiguous, and unified diffs without
that header (quilt, `diff -u`, `Index:` blocks) by their `---` / `+++`
pair plus a hunk header. Prose-shaped mentions
("we should touch fs/foo/") are deliberately ignored, high-noise
and lousy precision. The cost is missing discussion-only threads in
the per-path reverse lookup; the win is that every match is a real
patch hunk.

Each returned path is the **`b/` path**, the post-rename
destination when the patch renames a file, the only path when it
doesn't. This matches what reviewers and `MAINTAINERS` globs expect:
the new location of the code. The `a/` path is recoverable from the
blob if a future surface ever needs it; ingest time is the wrong
moment to make that choice durable.
"""

import logging
import re
from collections.abc import Callable
from functools import partial

from sqlalchemy import exists, select

from mimir._backfill import BackfillCounters, walk_articles
from mimir.backfill_runs import Run
from mimir.models import Article, ArticleFile
from mimir.parser import unfold
from mimir.patch_revisions import parse_in_series_patch_subject
from mimir.store import read_first_readable

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

# A unified diff without git's header (quilt, `diff -u`, svn's `Index:`
# blocks): a `---` / `+++` pair at column 0 that is at once followed by
# a hunk header (#869). The hunk header is what keeps a bare `---`
# signature separator and a quoted or prose-shaped pair out. A path may
# carry a tab and a timestamp after it, which is dropped; an `Index:`
# block is read through its pair, the `Index:` and `===` lines add no
# path of their own. A path with a tab in it is not supported.
_UNIFIED_RE = re.compile(
    r"^--- (?P<old>[^\t\r\n]+)(?:\t[^\r\n]*)?\r?\n"
    r"\+\+\+ (?P<new>[^\t\r\n]+)(?:\t[^\r\n]*)?\r?\n"
    r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@",
    re.MULTILINE,
)


def _unified_path(old: str, new: str) -> str | None:
    """The path a `---` / `+++` pair names, as the git branch reports
    it: the new side without its `b/`, and for a deleted file (`+++
    /dev/null`) the old side without its `a/`."""
    old, new = old.strip(), new.strip()
    if new == "/dev/null":
        path = old.removeprefix("a/")
    else:
        path = new.removeprefix("b/")
    return None if path in ("", "/dev/null") else path


# Hard ceiling on the number of distinct paths one patch body can
# contribute to `article_files`. Real-world series rarely touch more
# than ~50 files in one mail; 1000 is several orders of magnitude
# above any non-pathological patch. The cap defends against a
# crafted body with synthetic `diff --git` headers that would
# otherwise flood `article_files` with bogus rows and degrade the
# per-subsystem fan-outs that join on `(path, article_id)`.
MAX_TOUCHED_PATHS = 1000


# A hunk header, `@@ -a[,b] +c[,d] @@`, capturing the two line counts.
# Nine digits at most: `int()` refuses a string over 4300 digits, and a
# longer count is not a real hunk anyway, so the line reads as malformed.
_HUNK_RE = re.compile(r"@@ -\d{1,9}(?:,(\d{1,9}))? \+\d{1,9}(?:,(\d{1,9}))? @@")


def extract_touched_paths(body: str | None) -> dict[str, tuple[int, int] | None]:
    """Map each post-rename path the patch body touches to the
    `(insertions, deletions)` its hunks make there (#939).

    The paths are the keys. Returns an empty dict on:
    - non-patch bodies (no `diff --git` header, no unified diff)
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

    Caps the paths at `MAX_TOUCHED_PATHS`, the first ones in the order
    below. Real-world series stay well under that ceiling; the cap
    exists to bound work from a hostile or malformed message.

    The counts are `_count_lines`'s; a file whose hunks are malformed
    maps to None."""
    if not body:
        return {}
    headers = [(m.start(), m.group("new")) for m in _DIFF_GIT_RE.finditer(body)]
    headers += [
        (m.start(), _unified_path(m.group("old"), m.group("new")))
        for m in _UNIFIED_RE.finditer(body)
    ]
    paths: set[str] = set()
    for _, path in headers:
        if len(paths) >= MAX_TOUCHED_PATHS:
            break
        if path is not None:
            paths.add(path)
    headers.sort()
    return _count_lines(body, paths, headers)


def _count_lines(
    body: str, paths: set[str], headers: list[tuple[int, str | None]]
) -> dict[str, tuple[int, int] | None]:
    """`(insertions, deletions)` per path, from the hunks after each of
    its file headers (`headers`: `(offset, path)`, sorted).

    Hunks and not the diffstat: the diffstat's per-file bar is scaled
    and its paths truncated, and quilt and `diff -u` bodies have none.
    Each hunk is read by its header's line counts, which is what keeps
    the `-- ` signature after the last hunk from counting as a deleted
    line. A hunk belongs to the nearest header before it; one under a
    path past the cap, or under no header, is read and not counted.

    A file with a header and no hunk (binary, pure rename, mode change)
    counts 0/0, as `git diff --shortstat` does. A hunk that ends early,
    holds a line that is no hunk line, or whose header does not parse,
    makes its file None: a partial count would be shown as the total.
    An empty line is a context line whose space a mailer stripped, which
    `git apply` accepts too.

    One pass over the lines. Split on `\n` only, as the header regexes
    are: a form feed inside a source line must not end it."""
    counts: dict[str, list[int] | None] = {p: [0, 0] for p in paths}
    tally: list[int] | None = None  # the current file's [insertions, deletions]
    path: str | None = None
    nxt = 0
    old = new = 0  # lines the open hunk still owes, on each side
    offset = 0
    for line in body.split("\n"):
        start = offset
        offset += len(line) + 1
        line = line.removesuffix("\r")
        if old or new:
            tag = line[:1]
            if tag in (" ", ""):
                old, new = old - 1, new - 1
            elif tag == "-":
                old -= 1
                if tally is not None:
                    tally[1] += 1
            elif tag == "+":
                new -= 1
                if tally is not None:
                    tally[0] += 1
            elif tag == "\\":
                continue
            else:
                old = -1
            if old >= 0 and new >= 0:
                continue
            # Malformed: the file loses its count, and the line is read
            # again as a line outside any hunk (it may be the next header).
            old = new = 0
            if path is not None:
                counts[path] = tally = None
        while nxt < len(headers) and headers[nxt][0] <= start:
            path = headers[nxt][1]
            path = path if path in paths else None
            tally = counts[path] if path is not None else None
            nxt += 1
        if not line.startswith("@@"):
            continue
        m = _HUNK_RE.match(line)
        if m is None:
            if path is not None:
                counts[path] = tally = None
            continue
        old = int(m[1]) if m[1] is not None else 1
        new = int(m[2]) if m[2] is not None else 1
    if (old or new) and path is not None:
        counts[path] = None  # the body ended inside a hunk
    return {p: (c[0], c[1]) if c is not None else None for p, c in counts.items()}


def has_article_files(session, article_id: int) -> bool:
    """Whether `article_files` holds a row for the article: one seek on
    the primary key, without pulling the rows."""
    return (
        session.execute(
            select(ArticleFile.article_id)
            .where(ArticleFile.article_id == article_id)
            .limit(1)
        ).first()
        is not None
    )


def article_has_files():
    """`has_article_files` as an ORM column expression over `Article`,
    for readers that decide per row in one statement (the slot rule,
    `patch_series.slot_rank`). One predicate, two spellings: an
    `article_files` row exists."""
    return exists().where(ArticleFile.article_id == Article.id)


class BackfillResult(BackfillCounters):
    """Outcome counters for `backfill_article_files`. Every article
    examined lands in exactly one bucket. `skipped` covers articles
    that already had ArticleFile rows (idempotent re-runs), articles
    whose mirror is unreachable from this host, and, under
    `patch_subjects`, articles whose subject is not an in-series patch.
    """

    examined: int = 0
    indexed: int = 0  # had a patch body and one or more paths landed
    no_diff: int = 0  # body parsed but no diff found
    skipped: int = 0  # already had rows, unreadable, or not a patch subject
    failed: int = 0  # parse error reading the blob


def backfill_article_files(
    limit: int | None = None,
    reprocess: bool = False,
    progress: Callable[[BackfillResult], None] | None = None,
    *,
    max_seconds: float | None = None,
    run: Run | None = None,
    patch_subjects: bool = False,
) -> BackfillResult:
    """Walk articles, extract diff-touched paths, insert ArticleFile
     rows. Idempotent: articles with existing rows are skipped unless
     `reprocess=True` (which deletes existing rows before re-extracting
    , useful after an extractor change).

     Newest-first ordering so a `--limit`-bounded session covers the
     most-recently-active articles first; that's where the
     subsystem-header surface is most visible to a user.

     `max_seconds` + `run` enable broker-side cooperative scheduling
     (Phase 2.2): the broker handler runs at most one chunk per RPC,
     starting below the run's cursor (`mimir.backfill_runs`), then
     returns `partial=True` so the CLI loops with a follow-up RPC.
     Direct callers leave both None and get the original full-walk
     behaviour.

     `patch_subjects` reads the body only of articles whose subject
     parses as an in-series patch (#874), the only ones the reply rule
     reads `article_files` for. The rest are counted `skipped` from
     the subject alone, so the walk still pages every row but fetches
     no blob for them. It composes with `reprocess`: the patches are
     re-extracted, every other article keeps its rows."""
    from mimir._pending_backfill import _submit_article_files_batch

    result = BackfillResult()
    result.partial = walk_articles(
        result,
        partial(_process_one, patch_subjects=patch_subjects),
        _submit_article_files_batch,
        limit=limit,
        reprocess=reprocess,
        progress=progress,
        label="backfill_article_files",
        max_seconds=max_seconds,
        run=run,
    )
    return result


def _process_one(
    session, article: Article, reprocess: bool, *, patch_subjects: bool = False
) -> tuple[str, object]:
    """Per-article work for `backfill_article_files`. Returns
    `(bucket, pending_payload)` where pending_payload is an
    `_ArticleFilesPending` or None for buckets that produce no writes."""
    from mimir._pending_backfill import _ArticleFilesPending

    # A string check on the loaded row, so it goes before the row seek
    # and the blob read it saves.
    if (
        patch_subjects
        and parse_in_series_patch_subject(unfold(article.subject)) is None
    ):
        return "skipped", None

    has_rows = has_article_files(session, article.id)
    if has_rows and not reprocess:
        return "skipped", None

    # Re-read the body from the canonical inbox when the article is
    # filed there, else from another inbox it is filed in.
    # `canonical_inbox_id` can name an inbox with no row for the
    # article (#662), and `read_message` only finds filed ones.
    try:
        read = read_first_readable(session, [article], article.canonical_inbox_id)
    except KeyError:
        return "skipped", None
    except Exception as exc:
        logger.warning(
            "backfill: parse failure for article %d (%s): %r",
            article.id,
            article.message_id,
            exc,
        )
        return "failed", None
    if read is None:
        # No filed inbox's mirror has the blob on this host (missing
        # epoch, or a recorded SHA that is not in the local repo).
        # Common in dev and after a partial-mirror rebuild; defer the
        # work rather than fail loudly, a re-run from a host with the
        # full mirror picks the article up.
        return "skipped", None
    parsed = read[1]

    files = extract_touched_paths(parsed.body)
    if not files:
        # An older extractor may have stored rows for this body; with no
        # diff to replace them, reprocess must still drop them (#923).
        if has_rows:
            return "no_diff", _ArticleFilesPending(
                article_id=article.id, delete_first=True
            )
        return "no_diff", None

    return "indexed", _ArticleFilesPending(
        article_id=article.id,
        delete_first=(has_rows and reprocess),
        files=files,
    )


# Re-export for callers (CLI) so the import surface stays tight.
__all__ = [
    "BackfillResult",
    "backfill_article_files",
    "extract_touched_paths",
]
