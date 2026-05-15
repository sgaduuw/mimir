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

from mimir._backfill import walk_articles

logger = logging.getLogger(__name__)


# `^[<inside>] <title>`, only matches when the bracket starts the
# subject, so `Re: [PATCH ...]` replies don't masquerade as cover
# letters. The original cover letter has nothing before the `[`.
_BRACKET_RE = re.compile(r"^\[([^\]]+)\]\s*(.+)$")

# `0/N` shape inside the bracket, the cover-letter discriminator.
# `\b` boundaries so a stray `10/30` (patch ten in series of thirty)
# can't be misread as a cover letter.
_ZERO_OF_N_RE = re.compile(r"\b0\s*/\s*\d+\b")

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
    """Outcome counters for `backfill_patch_series`."""
    examined: int = 0
    indexed: int = 0      # cover letter detected, key + version written
    not_cover: int = 0    # non-cover-letter subject, no series row
    skipped: int = 0      # already had a key set (idempotent re-run)


def _process_one(session, article, reprocess: bool) -> str:
    """Per-article work for `backfill_patch_series`. Returns the
    bucket name on `BackfillResult` to bump. `session` is unused
    here (no body re-read, no related-row delete) but the walker
    contract passes it; it stays available for a future slice
    that wants per-revision link rows."""
    if article.patch_series_key is not None and not reprocess:
        return "skipped"
    cover = parse_cover_letter(article.subject)
    if cover is None:
        # Reprocess: clear any prior key (the subject may have
        # changed, or our parser may now reject something it
        # previously accepted).
        if reprocess and article.patch_series_key is not None:
            article.patch_series_key = None
            article.patch_series_version = None
        return "not_cover"
    article.patch_series_key = series_key(cover.title, article.author)
    article.patch_series_version = cover.version
    return "indexed"


def backfill_patch_series(
    limit: int | None = None,
    reprocess: bool = False,
    progress: Callable[["BackfillResult"], None] | None = None,
) -> BackfillResult:
    """Walk articles, parse subjects for cover-letter shape, write
    `patch_series_key` + `patch_series_version` where applicable.

    Cheaper than `backfill_article_files`: only reads `Article.subject`
    and `Article.author`, no body re-parse via the git mirror. Safe
    to run on any host with the DB; doesn't need the inbox mirrors.
    `preload_lists=False` is the saving, patches and trailers need
    `Article.lists` for the inbox lookup; we don't.

    Idempotent: articles with `patch_series_key` already set are
    skipped unless `reprocess=True`. Newest-first walk so a
    bounded session covers the most-visible articles first.
    """
    result = BackfillResult()
    walk_articles(
        result, _process_one,
        limit=limit, reprocess=reprocess, progress=progress,
        preload_lists=False,
    )
    return result


__all__ = [
    "BackfillResult",
    "CoverLetter",
    "backfill_patch_series",
    "parse_cover_letter",
    "series_key",
]
