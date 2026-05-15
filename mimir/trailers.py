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
from typing import Callable

from pydantic import BaseModel
from sqlalchemy import select

from mimir._backfill import walk_articles
from mimir.models import Article, ArticleTrailer, Inbox
from mimir.store import MessageNotFound, read_message

logger = logging.getLogger(__name__)


# The roles we index. See module docstring for what's deliberately
# excluded. Order is significant only insofar as the canonical-
# casing dict iterates it; the regex alternation is order-agnostic
# because each role is a literal alternative.
INDEXED_TRAILER_ROLES = (
    "Reviewed-by",
    "Acked-by",
    "Tested-by",
    "Reported-by",
    "Suggested-by",
    "Co-developed-by",
    "Reported-and-tested-by",
)
_INDEXED_TRAILER_RE = re.compile(
    r"^(?P<role>" + "|".join(re.escape(k) for k in INDEXED_TRAILER_ROLES) + r"):\s*"
    r"(?P<rest>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Match "Display Name <addr@host>" or bare "<addr@host>" / "addr@host".
# The local-part / domain subset matches the conservative shape used
# elsewhere (see rendering._EMAIL_ANGLE_RE) so address-shaped tokens
# carrying HTML metacharacters fall through and the line is silently
# skipped. The address itself is stored verbatim; redaction is a
# render-time concern (see CONTEXT.md "Redaction is a display-time
# decision").
_TRAILER_NAME_ADDR_RE = re.compile(
    r"^(?:(?P<name>.*?)\s*)?<?(?P<addr>[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+)>?\s*$"
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
        addr_match = _TRAILER_NAME_ADDR_RE.match(m.group("rest"))
        if addr_match is None:
            continue
        name = (addr_match.group("name") or "").strip()
        # Strip wrapping quotes a prose line might add around the
        # display name.
        if name.startswith('"') and name.endswith('"') and len(name) >= 2:
            name = name[1:-1].strip()
        address = addr_match.group("addr")
        out.append((canonical[m.group("role").lower()], name, address))
    return out


class BackfillResult(BaseModel):
    """Outcome counters for `backfill_article_trailers`. Each examined
    article lands in exactly one bucket. `skipped` covers articles
    that already have rows (idempotent re-runs) plus mirror-
    unreachable cases."""
    examined: int = 0
    indexed: int = 0      # body parsed and one or more trailer rows landed
    no_trailers: int = 0  # body parsed but no indexed-role trailers
    skipped: int = 0      # already had rows, or unreadable
    failed: int = 0       # parse error reading the blob


def backfill_article_trailers(
    limit: int | None = None,
    reprocess: bool = False,
    progress: Callable[["BackfillResult"], None] | None = None,
) -> BackfillResult:
    """Walk articles, extract trailers, insert ArticleTrailer rows.
    Idempotent: articles with existing rows are skipped unless
    `reprocess=True` (which deletes existing rows first, useful
    after an extractor change).

    Newest-first ordering so a `--limit`-bounded session covers the
    most-recently-active articles first; the per-author / per-
    subsystem reviewer surfaces (slices 2+3) gain visible coverage
    fastest from the recent end."""
    result = BackfillResult()
    walk_articles(
        result, _process_one,
        limit=limit, reprocess=reprocess, progress=progress,
    )
    return result


def _process_one(session, article: Article, reprocess: bool) -> str:
    """Handle one article. Returns the bucket name it lands in."""
    has_rows = session.execute(
        select(ArticleTrailer.id)
        .where(ArticleTrailer.article_id == article.id)
        .limit(1)
    ).first() is not None
    if has_rows and not reprocess:
        return "skipped"
    if has_rows and reprocess:
        session.execute(
            ArticleTrailer.__table__.delete()
            .where(ArticleTrailer.__table__.c.article_id == article.id)
        )

    # Prefer canonical_inbox (deterministic for cross-posts) over
    # the SQLA-load-ordering-dependent `article.lists[0]`. See the
    # matching helper in `mimir.patches` for the rationale.
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
        # Mirror unreachable on this host; defer rather than fail.
        return "skipped"
    except Exception as exc:
        logger.warning(
            "backfill: parse failure for article %d (%s): %r",
            article.id, article.message_id, exc,
        )
        return "failed"

    trailers = extract_trailers(parsed.body)
    if not trailers:
        return "no_trailers"
    for role, name, address in trailers:
        session.add(ArticleTrailer(
            article_id=article.id,
            role=role,
            name=name,
            address=address,
            address_normalized=address.lower(),
        ))
    return "indexed"


__all__ = [
    "BackfillResult",
    "INDEXED_TRAILER_ROLES",
    "backfill_article_trailers",
    "extract_trailers",
]
