"""Review-attestation trailer indexing: extract `Reviewed-by:`,
`Acked-by:`, `Tested-by:`, etc. lines from a message body and a
backfill walker for the `article_trailers` table.

Operates on `parsed.body` (the decoded plain-text body, already
surrogate-scrubbed). Pure extractor; no DB or git deps. The caller
(`mimir.ingest` and the backfill CLI) is responsible for turning
the returned tuples into `ArticleTrailer` rows.

Strong signal only: anchored at line start so re-quoted trailers
(`> Reviewed-by: …` from a parent message) don't double-count.
Same precision posture as `mimir.patches.extract_touched_paths`.

Excludes Signed-off-by deliberately (see `INDEXED_TRAILER_ROLES`):
SoB is the author's own DCO chain-of-custody marker, not a *review*
signal, and the author is already on Article.author. Cc/To/From in
trailer position are routing leftovers, not attestations.
"""

import logging
import re
from collections.abc import Callable

from sqlalchemy import select

from mimir._backfill import BackfillCounters, walk_articles
from mimir.backfill_runs import Run
from mimir.models import Article, ArticleTrailer
from mimir.rendering.linkify import EMAIL_LOCAL_PART
from mimir.store import read_first_readable

logger = logging.getLogger(__name__)


# Review feedback excludes authorship and bug-report attribution.
REVIEW_TRAILER_ROLES = ("Reviewed-by", "Acked-by", "Tested-by")

# The roles we index. See module docstring for what's deliberately
# excluded. Order is significant only insofar as the canonical-
# casing dict iterates it; the regex alternation is order-agnostic
# because each role is a literal alternative.
INDEXED_TRAILER_ROLES = (
    *REVIEW_TRAILER_ROLES,
    "Reported-by",
    "Suggested-by",
    "Co-developed-by",
    "Reported-and-tested-by",
)
_INDEXED_TRAILER_RE = re.compile(
    r"^(?P<role>" + "|".join(re.escape(k) for k in INDEXED_TRAILER_ROLES) + r"):\s*"
    r"(?P<rest>.+)",
    re.IGNORECASE | re.MULTILINE,
)
# Match "Display Name <addr@host>" or bare "<addr@host>" / "addr@host",
# anchored at the line's end: the address is the last token, and the
# name is whatever precedes it (or its "<"). The local part is the one
# rendering uses, so parsing and display agree on what an address is.
# The address starts only after whitespace, after "<" or at the line's
# start. A token that reaches the "@" any other way (`A <x"y@...>`,
# `Name:a@...`) is skipped, not split inside it as the parser did
# before #913 (`X <x=` / `y@example.com`). The same boundary keeps the
# search linear: it never restarts inside a run (#921). `rest` keeps the
# line's trailing whitespace for the `\s*$` here, as a lazy `rest`
# before its own `\s*$` was quadratic on a whitespace run.
# The address is stored verbatim; redaction is a render-time concern
# (see CONTEXT.md "Redaction is a display-time decision").
_TRAILER_NAME_ADDR_RE = re.compile(
    r"(?:(?<!\S)<?|<)(?P<addr>" + EMAIL_LOCAL_PART + r"@[A-Za-z0-9.\-]+)>?\s*$"
)


def extract_trailers(body: str | None) -> list[tuple[str, str, str]]:
    """Extract review-attestation trailers from a message body.

    Returns a list of (role, name, address) tuples, one per matched
    line. `role` is normalised to the canonical capitalisation from
    `INDEXED_TRAILER_ROLES`; `name` is the display name (empty string
    when the line carried only an address); `address` is the verbatim
    address from the trailer (the caller normalises for indexing).

    Quoted-block detection follows the same line-anchor approach as
    `extract_touched_paths`: the regex is `MULTILINE`-anchored at
    column 0, so `> Reviewed-by:` lines (re-quoted from a parent) are
    skipped. A reviewer who unquotes a trailer when responding (rare)
    has it counted, which is correct: that IS a fresh attestation.
    """
    if not body:
        return []
    canonical = {k.lower(): k for k in INDEXED_TRAILER_ROLES}
    out: list[tuple[str, str, str]] = []
    for m in _INDEXED_TRAILER_RE.finditer(body):
        rest = m.group("rest")
        addr_match = _TRAILER_NAME_ADDR_RE.search(rest)
        if addr_match is None:
            continue
        name = rest[: addr_match.start()].strip()
        # Strip wrapping quotes a prose line might add around the
        # display name.
        if name.startswith('"') and name.endswith('"') and len(name) >= 2:
            name = name[1:-1].strip()
        address = addr_match.group("addr")
        out.append((canonical[m.group("role").lower()], name, address))
    return out


class BackfillResult(BackfillCounters):
    """Outcome counters for `backfill_article_trailers`. Each examined
    article lands in exactly one bucket. `skipped` covers articles
    that already have rows (idempotent re-runs) plus mirror-
    unreachable cases.
    """

    examined: int = 0
    indexed: int = 0  # body parsed and one or more trailer rows landed
    no_trailers: int = 0  # body parsed but no indexed-role trailers
    skipped: int = 0  # already had rows, or unreadable
    failed: int = 0  # parse error reading the blob


def backfill_article_trailers(
    limit: int | None = None,
    reprocess: bool = False,
    progress: Callable[[BackfillResult], None] | None = None,
    *,
    max_seconds: float | None = None,
    run: Run | None = None,
) -> BackfillResult:
    """Walk articles, extract trailers, insert ArticleTrailer rows.
    Idempotent: articles with existing rows are skipped unless
    `reprocess=True` (which deletes existing rows first, useful
    after an extractor change).

    Newest-first ordering so a `--limit`-bounded session covers the
    most-recently-active articles first; the per-author / per-
    subsystem reviewer surfaces (slices 2+3) gain visible coverage
    fastest from the recent end.

    `max_seconds` + `run` enable broker-side cooperative
    scheduling (Phase 2.2); see `mimir.patches.backfill_article_files`
    for the chunk/resume contract. Direct callers leave both None."""
    from mimir._pending_backfill import _submit_article_trailers_batch

    result = BackfillResult()
    result.partial = walk_articles(
        result,
        _process_one,
        _submit_article_trailers_batch,
        limit=limit,
        reprocess=reprocess,
        progress=progress,
        label="backfill_article_trailers",
        max_seconds=max_seconds,
        run=run,
    )
    return result


def _process_one(session, article: Article, reprocess: bool) -> tuple[str, object]:
    """Per-article work for `backfill_article_trailers`. Returns
    `(bucket, pending_payload)` where pending_payload is None for
    no-write buckets, otherwise an `_ArticleTrailersPending` instance
    (with no trailers when reprocess only deletes stale rows).

    The return-type annotation is `tuple[str, object]` (not the more
    precise `tuple[str, _ArticleTrailersPending | None]`) because
    `_ArticleTrailersPending` is imported lazily inside the function;
    ruff F821 would flag the precise annotation as undefined at
    module scope.
    """
    from mimir._pending_backfill import (
        _ArticleTrailerInsert,
        _ArticleTrailersPending,
    )

    has_rows = (
        session.execute(
            select(ArticleTrailer.id)
            .where(ArticleTrailer.article_id == article.id)
            .limit(1)
        ).first()
        is not None
    )
    if has_rows and not reprocess:
        return "skipped", None

    # Canonical inbox when the article is filed there, else another
    # filed inbox (#662: the stored id can name one with no row).
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
        return "skipped", None
    parsed = read[1]

    trailers = extract_trailers(parsed.body)
    if not trailers:
        # Reprocess still clears rows an older parser stored from a line
        # this one skips (#913).
        if has_rows and reprocess:
            return "no_trailers", _ArticleTrailersPending(
                article_id=article.id, delete_first=True
            )
        return "no_trailers", None

    return "indexed", _ArticleTrailersPending(
        article_id=article.id,
        delete_first=(has_rows and reprocess),
        trailers=[
            _ArticleTrailerInsert(role=r, name=n, address=a) for r, n, a in trailers
        ],
    )


__all__ = [
    "INDEXED_TRAILER_ROLES",
    "REVIEW_TRAILER_ROLES",
    "BackfillResult",
    "backfill_article_trailers",
    "extract_trailers",
]
