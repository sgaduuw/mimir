"""Patch-series detection.

A "patch series" is a maintainer-flavour grouping: the same logical
proposal posted multiple times (v1, v2, v3, ...) as feedback rolls
in. This module identifies *cover letters*, the `0/N` message that
introduces each revision, and computes a stable key so multiple
revisions of the same series can be cross-linked.

Slice 1 deliberately handles cover letters only. Per-patch
(non-cover-letter) attachment to a series is harder, subjects
drift between revisions (1/N reorder, drop, rename), so robust
linking needs more than the normalised-title heuristic that's
enough here. That work lives in a future slice.

Pure functions only. The DB-side wiring (ingest writes the key,
the message view reads it) is in `mimir.ingest` and `mimir.web`.
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Callable

from pydantic import BaseModel
from sqlalchemy import select

from mimir._backfill import walk_articles
from mimir.models import Article
from mimir.patch_revisions import parse_in_series_patch_subject

logger = logging.getLogger(__name__)


# `^[<inside>] <title>`, only matches when the bracket starts the
# subject, so `Re: [PATCH ...]` replies don't masquerade as cover
# letters. The original cover letter has nothing before the `[`.
_BRACKET_RE = re.compile(r"^\[([^\]]+)\]\s*(.+)$")

# `0/N` shape inside the bracket, the cover-letter discriminator.
# `0+` (not just `0`) so zero-padded covers like `00/24` or `000/100`
# still match: contributors using `git format-patch --numbered`
# get column-aligned shapes (`00/27`, `01/27`, ..., `27/27`) when
# the series has ≥10 patches, and we want those covers indexed too.
# `\b` boundaries so a stray `10/30` (patch ten in series of thirty)
# can't be misread as a cover letter.
_ZERO_OF_N_RE = re.compile(r"\b0+\s*/\s*\d+\b")

# Version qualifiers. `v\d+` is the conventional revision marker;
# `RFC` and `RESEND` are version-shaped enough to surface
# distinctly. Anything else falls back to v1 (the unmarked first
# version is implicit).
_VERSION_RE = re.compile(r"\b(v\d+|RFC|RESEND)\b", re.IGNORECASE)

# Bracket must literally contain the word "PATCH", there are
# `[GIT PULL]` and `[ANNOUNCE]` subjects that carry `0/N` for
# unrelated reasons; the PATCH token disambiguates.
_PATCH_TOKEN_RE = re.compile(r"\bPATCH\b")


@dataclass(frozen=True)
class CoverLetter:
    """One detected cover letter. `version` is normalised to a
    lowercase string like `v1`, `v2`, `rfc`, `resend`, that's
    what gets stored on the Article row and rendered on the
    timeline."""
    version: str
    total: int
    title: str


def parse_cover_letter(subject: str | None) -> CoverLetter | None:
    """Detect whether `subject` opens a patch series cover letter.

    Returns `None` for non-cover-letters (every non-patch subject,
    every per-patch `1/N`+ subject, and every `Re:` reply since the
    bracket-at-start anchor doesn't match those).

    Recognises:
        [PATCH 0/3] title here          → v1
        [PATCH v2 0/3] title here       → v2
        [PATCH RFC 0/3] title here      → rfc
        [PATCH RESEND v3 0/3] title     → v3   (RESEND ignored, real version wins)
        [PATCH net 0/3] title           → v1   (subsystem tag tolerated)
        [PATCH net-next v2 0/12] title  → v2

    Doesn't recognise:
        Re: [PATCH v2 0/3] title       , bracket isn't at column 0
        [PATCH 1/3] title              , not a cover letter
        [ANNOUNCE 0/3] title           , no PATCH token
    """
    if not subject:
        return None
    m = _BRACKET_RE.match(subject.strip())
    if m is None:
        return None
    inside, title = m.group(1), m.group(2).strip()
    if not _PATCH_TOKEN_RE.search(inside):
        return None
    zero_of = _ZERO_OF_N_RE.search(inside)
    if zero_of is None:
        return None
    try:
        total = int(zero_of.group(0).split("/")[1].strip())
    except (ValueError, IndexError):
        return None
    # Prefer the most specific version marker: explicit v\d+ over
    # RFC/RESEND. A subject like `[PATCH RESEND v3 0/3]` is a resent
    # v3, not RFC-shaped. The old for/else form happened to work
    # because of `findall`'s ordering, but the precedence wasn't
    # explicit. `next()` with a v\d+ filter + a marker fallback +
    # the "v1" default makes the precedence read off the line.
    versions = _VERSION_RE.findall(inside)
    version = next(
        (v.lower() for v in versions if v.lower().startswith("v")),
        versions[0].lower() if versions else "v1",
    )
    return CoverLetter(version=version, total=total, title=title)


def series_key(title: str, author: str | None) -> str:
    """Stable opaque key for a series identity.

    Combines (normalised title, author email) and SHA-1s it for a
    fixed-length string. Two cover letters with the same title +
    author always produce the same key; different title or
    different author produce different keys.

    The hash is deliberate: storing the raw `<author>|<title>`
    string would expose the author's email in any query or
    diagnostic dump, defeating the visible-HTML redaction posture
    the rest of the app maintains for non-allowlisted senders. The
    SHA is opaque, machine-readable only, and joins cheaply.
    """
    # Strip the author's display name; we want the email-address
    # half so renames don't break linkage.
    _name, address = parseaddr(author or "")
    address = address.lower().strip()
    # Title normalisation: lowercase + whitespace collapse. We
    # don't strip bracketed tags because the cover-letter parser
    # already chopped them off, the title arg is the post-bracket
    # text. Just normalise for case + whitespace drift.
    norm_title = " ".join(title.lower().split())
    payload = f"{address}|{norm_title}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


class BackfillResult(BaseModel):
    """Outcome counters for `backfill_patch_series`.

    `partial` + `continuation` carry the cooperative-scheduling
    handoff between broker chunks (Phase 2.2). Direct (non-broker)
    callers always see `partial=False, continuation=None`.
    """
    examined: int = 0
    indexed: int = 0           # cover letter detected, key + version + pos=0 written
    in_series_indexed: int = 0  # in-series patch linked to its cover via thread parent
    in_series_orphan: int = 0   # in-series patch, position set, cover not in DB
    not_cover: int = 0          # non-series-shaped subject (prose, replies, solo [PATCH])
    skipped: int = 0            # already fully resolved (idempotent re-run)
    partial: bool = False
    continuation: int | None = None

    def merge(self, other: "BackfillResult") -> "BackfillResult":
        """Sum counters with `other`, carrying `other`'s
        `partial`/`continuation` forward."""
        return BackfillResult(
            examined=self.examined + other.examined,
            indexed=self.indexed + other.indexed,
            in_series_indexed=self.in_series_indexed + other.in_series_indexed,
            in_series_orphan=self.in_series_orphan + other.in_series_orphan,
            not_cover=self.not_cover + other.not_cover,
            skipped=self.skipped + other.skipped,
            partial=other.partial,
            continuation=other.continuation,
        )


def _resolve_in_series_link(
    session, article,
) -> tuple[str, str] | None:
    """Look up `(patch_series_key, patch_series_version)` from the
    article's thread parent. Depth-1 walk: `git send-email --thread`
    posts in-series patches as direct children of the cover, which
    is the modal shape. Deeper chains (patch resent as a reply to
    a reply) fall through to the orphan bucket; if they become
    common we'll lift the depth, but for now the cost of a recursive
    walker isn't worth the rare hit.

    Two-step resolve so a single backfill pass is order-independent:
    if the parent is already keyed, use those values; otherwise
    compute the key + version from the parent's subject locally
    (without writing). When the walker later visits the parent and
    runs the cover-letter path, it writes the same `series_key()`
    output, so the in-series patch and cover converge on the same
    key without ordering constraints.
    """
    if not article.thread_parent:
        return None
    parent = session.execute(
        select(
            Article.subject,
            Article.author,
            Article.patch_series_key,
            Article.patch_series_version,
        ).where(Article.message_id == article.thread_parent)
    ).one_or_none()
    if parent is None:
        return None
    if parent.patch_series_key is not None:
        return parent.patch_series_key, parent.patch_series_version
    cover = parse_cover_letter(parent.subject)
    if cover is None:
        return None
    return series_key(cover.title, parent.author), cover.version


def _process_one(session, article, reprocess: bool) -> str:
    """Per-article work for `backfill_patch_series`. Returns the
    bucket name on `BackfillResult` to bump.

    Idempotency: an article is "fully resolved" when its
    `patch_series_position` is set AND (it's a cover with key, OR
    it's an in-series patch with key). An in-series patch with
    position but NULL key is an orphan that we try to re-link on
    every run, the cover may have arrived since the prior pass.
    """
    # Fully-resolved articles skip on non-reprocess runs.
    is_fully_resolved = (
        article.patch_series_position is not None
        and (
            article.patch_series_position == 0
            and article.patch_series_key is not None
            or article.patch_series_position > 0
            and article.patch_series_key is not None
        )
    )
    if is_fully_resolved and not reprocess:
        return "skipped"

    cover = parse_cover_letter(article.subject)
    if cover is not None:
        article.patch_series_key = series_key(cover.title, article.author)
        article.patch_series_version = cover.version
        article.patch_series_position = 0
        return "indexed"

    in_series = parse_in_series_patch_subject(article.subject)
    if in_series is not None:
        article.patch_series_position = in_series.position
        link = _resolve_in_series_link(session, article)
        if link is not None:
            article.patch_series_key, article.patch_series_version = link
            return "in_series_indexed"
        # Orphan: clear key/version on reprocess (a prior pass may
        # have linked, but the cover got rewritten or removed since).
        if reprocess:
            article.patch_series_key = None
            article.patch_series_version = None
        return "in_series_orphan"

    # Neither a cover nor an in-series patch. Reprocess clears any
    # prior series row (subject may have changed, or our parsers
    # tightened a previously-accepted shape).
    if reprocess and (
        article.patch_series_key is not None
        or article.patch_series_position is not None
    ):
        article.patch_series_key = None
        article.patch_series_version = None
        article.patch_series_position = None
    return "not_cover"


def backfill_patch_series(
    limit: int | None = None,
    reprocess: bool = False,
    progress: Callable[["BackfillResult"], None] | None = None,
    *,
    max_seconds: float | None = None,
    start_cursor: int | None = None,
) -> BackfillResult:
    """Walk articles, parse subjects for cover-letter / in-series
    shape, write `patch_series_key` + `patch_series_version` +
    `patch_series_position` where applicable.

    Cheaper than `backfill_article_files`: only reads `Article.subject`
    + `Article.author` + `Article.thread_parent`, no body re-parse via
    the git mirror. Safe to run on any host with the DB; doesn't need
    the inbox mirrors. `preload_lists=False` is the saving, patches
    and trailers need `Article.lists` for the inbox lookup; we don't.

    Idempotent: fully-resolved articles are skipped unless
    `reprocess=True`. In-series patch orphans (position set, no
    cover known) are re-attempted every run so a later-ingested
    cover letter links the previously-orphaned patches without a
    `--reprocess` sweep. Newest-first walk so a bounded session
    covers the most-visible articles first.

    In-series detection is a #212 addition; pre-#212 runs only
    keyed cover letters.

    `max_seconds` + `start_cursor` enable broker-side cooperative
    scheduling (Phase 2.2); see `mimir.patches.backfill_article_files`
    for the chunk/resume contract. Direct callers leave both None.
    """
    result = BackfillResult()
    partial, continuation = walk_articles(
        result, _process_one,
        limit=limit, reprocess=reprocess, progress=progress,
        preload_lists=False,
        label="backfill_patch_series",
        max_seconds=max_seconds, start_cursor=start_cursor,
    )
    result.partial = partial
    result.continuation = continuation
    return result


__all__ = [
    "BackfillResult",
    "CoverLetter",
    "backfill_patch_series",
    "parse_cover_letter",
    "series_key",
]
