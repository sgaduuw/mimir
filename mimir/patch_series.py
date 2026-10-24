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
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parseaddr

from sqlalchemy import and_, literal_column, select

from mimir._backfill import BackfillCounters, walk_articles
from mimir.backfill_runs import Run
from mimir.models import Article
from mimir.parser import unfold
from mimir.patch_revisions import parse_in_series_patch_subject, patch_bracket
from mimir.patches import has_article_files

logger = logging.getLogger(__name__)


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
# version is implicit). A `v\d` glued to PATCH, PATCHES, PATCHSET, RFC
# or RESEND (`[PATCHv2`, `[RFCv9`) also starts a marker, and RFC and
# RESEND may end at it (#816, #819). A dotted version followed by `-`
# (`v2019.04-aspeed`, `v6.12-rc1`) names a base tree, not a revision, and
# so does a parenthesised one (`(v20.08)`, #870). A dotted version
# (`v5.10`) is the revision only when no plain `vN` follows it
# (`_bracket_version`).
_VERSION_RE = re.compile(
    r"(?<!\()(?:\b|(?:(?<=\bPATCH)|(?<=\bPATCHES)|(?<=\bPATCHSET)|(?<=\bRFC)|(?<=\bRESEND))(?=v\d))"
    r"(v\d+(?!(?:\.\d+)+[-)])|RFC|RESEND)(?:\b|(?=v\d))",
    re.IGNORECASE,
)


def version_number_sql(column: str) -> str:
    """SQL expression for the revision number of a stored version, as
    `parse_cover_letter` writes it: `vN` is N, `rfc` and `resend` are 1
    (the implicit first version), NULL stays NULL. Lives here, beside
    the parser, so the format and its reader change together (#677:
    `CAST('v2' AS INTEGER)` is 0 in SQLite)."""
    return (
        f"CASE WHEN {column} LIKE 'v%' THEN CAST(SUBSTR({column}, 2) AS INTEGER)"
        f" WHEN {column} IS NOT NULL THEN 1 END"
    )


# A RESEND in a leading bracket, ahead of the one `patch_bracket` picks:
# `[RESEND] [PATCH v3 0/2] title`.
_RESEND_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)*\[[^\]]*\bRESEND\b", re.IGNORECASE)


def posting_flags(subject: str | None, version: str | None) -> tuple[bool, bool]:
    """`(is_rfc, is_resend)` of a cover posting. The stored version
    keeps only one marker (`[PATCH RFC v2]` is `v2`, `[PATCH RESEND v3]`
    is `v3`), so the brackets are read again; the stored `rfc` and
    `resend` cover a subject that no longer parses."""
    subject = unfold(subject or "")
    bracket = patch_bracket(subject)
    markers = {m.lower() for m in _VERSION_RE.findall(bracket[0])} if bracket else set()
    return (
        "rfc" in markers or version == "rfc",
        "resend" in markers
        or version == "resend"
        or _RESEND_RE.match(subject) is not None,
    )


def chain_version_rank(version: str | None, is_rfc: bool) -> tuple[int, int]:
    """Order of the postings of one series, for chaining (#816): an RFC
    ranks below every non-RFC version, so `[RFC]` followed by `[PATCH
    v1]` is a version up and joins one chain. The lifecycle pill does
    the opposite on purpose: `version_number_sql` folds RFC to 1 so an
    RFC and the v1 it became show as one revision (#691), which would
    read that pair as a repeat of v1 here. A bare `resend` is v1, the
    implicit first version."""
    if version and version.startswith("v"):
        number = int(version[1:])
    else:
        number = 0 if version == "rfc" else 1
    return (0 if is_rfc else 1, number)


def representative_key(version: str | None, date, article_id: int) -> tuple:
    """Sort key choosing the row that names a folded posting (#691): a
    stored `vN` beats `rfc` and `resend` (the real submission, whatever
    the archive order), then the oldest by `(date, id)`. Both lists that
    fold on the revision number use it, so they pick the same posting.
    The `v` test is the `LIKE 'v%'` of `version_number_sql`."""
    numbered = version is not None and version.lower().startswith("v")
    return (not numbered, date is None, date, article_id)


# A series is (patch_series_key, COALESCE(patch_series_head_id, 0))
# (#816): one key can hold several series a sender posted under one
# cover title, and the head id tells them apart. NULL is the key's
# first series, so a NULL head everywhere means the key alone, which is
# how every row reads until the chain backfill runs. The helpers below
# are the only places the pair is spelled; readers that compare the key
# alone are caught by `tests/test_series_identity_single_owner.py`.


def series_head_sql(alias: str) -> str:
    """SQL expression for the series head of the article aliased
    `alias`, NULL as 0, for readers that select or group by it."""
    return f"COALESCE({alias}.patch_series_head_id, 0)"


def same_series_sql(a: str, b: str) -> str:
    """SQL condition: the articles aliased `a` and `b` are in one series."""
    return (
        f"{a}.patch_series_key = {b}.patch_series_key"
        f" AND {series_head_sql(a)} = {series_head_sql(b)}"
    )


def series_head():
    """`series_head_sql` for ORM queries over `Article`."""
    return literal_column(series_head_sql(Article.__tablename__))


def same_series(key: str, head_id: int | None):
    """ORM condition: an `Article` is in the series `(key, head_id)`."""
    return and_(Article.patch_series_key == key, series_head() == (head_id or 0))


def newer_revision_sql(older: str, newer: str) -> str:
    """SQL condition: the article aliased `newer` is a later revision
    of the patch aliased `older`. Same series, same position (NULL
    is 0, a cover before the #212 backfill), higher revision number.
    Since #212 every patch of a series carries the key, so a series
    match alone also matches the other patches of the same posting. One
    predicate for the lifecycle pill and the needs-attention list."""
    return (
        f"{older}.patch_series_key IS NOT NULL"
        f" AND {same_series_sql(newer, older)}"
        f" AND COALESCE({newer}.patch_series_position, 0)"
        f" = COALESCE({older}.patch_series_position, 0)"
        f" AND {version_number_sql(newer + '.patch_series_version')}"
        f" > {version_number_sql(older + '.patch_series_version')}"
    )


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
        [RFC 0/6] title                 → rfc  (no PATCH token, see `patch_bracket`)
        [net-next v7 00/10] title       → v7
        [U-Boot] [PATCH v2 0/3] title   → v2   (list tag; title and key exclude it)
        [patch 0/3] title               → v1   (PATCH in any case)

    Doesn't recognise:
        Re: [PATCH v2 0/3] title       , bracket isn't at column 0
        [PATCH 1/3] title              , not a cover letter
        [ANNOUNCE 0/3] title           , PULL and ANNOUNCE never count
        [linux-next:master 0/9] title  , a colon marks a build robot
        [0/1] title                    , no other word in the bracket
    """
    if not subject:
        return None
    bracket = patch_bracket(subject)
    if bracket is None:
        return None
    inside, title = bracket
    zero_of = _ZERO_OF_N_RE.search(inside)
    if zero_of is None:
        return None
    try:
        total = int(zero_of.group(0).split("/")[1].strip())
    except ValueError, IndexError:
        return None
    return CoverLetter(version=_bracket_version(inside), total=total, title=title)


def _bracket_version(inside: str) -> str:
    """The version a patch bracket names, as `parse_cover_letter` stores
    it."""
    # Prefer the most specific version marker: explicit v\d+ over
    # RFC/RESEND. A subject like `[PATCH RESEND v3 0/3]` is a resent
    # v3, not RFC-shaped. The old for/else form happened to work
    # because of `findall`'s ordering, but the precedence wasn't
    # explicit. `next()` with a v\d+ filter + a marker fallback +
    # the "v1" default makes the precedence read off the line.
    found = [
        (m.group(1).lower(), inside[m.end() :]) for m in _VERSION_RE.finditer(inside)
    ]
    numbers = [(v, rest) for v, rest in found if v.startswith("v")]
    # A plain `vN` beats a dotted one: `[PATCH v5.10 v2]` is v2 (#870).
    plain = [v for v, rest in numbers if not rest.startswith(".")]
    return next(iter(plain or [v for v, _ in numbers] or [v for v, _ in found]), "v1")


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
    address, norm_title = _series_identity(title, author)
    payload = f"{address}|{norm_title}".encode()
    return hashlib.sha1(payload).hexdigest()


def _series_identity(title: str, author: str | None) -> tuple[str, str]:
    """`(address, title)` as `series_key` hashes them, so
    `_repeats_parent` compares exactly what the key does."""
    # Strip the author's display name; we want the email-address
    # half so renames don't break linkage.
    _name, address = parseaddr(author or "")
    # Title normalisation: lowercase + whitespace collapse. We
    # don't strip bracketed tags because the cover-letter parser
    # already chopped them off, the title arg is the post-bracket
    # text. Just normalise for case + whitespace drift.
    return address.lower().strip(), " ".join(title.lower().split())


def _posting(subject: str | None) -> tuple[int, int, str, str] | None:
    """`(position, total, version, title)` of a cover or in-series patch
    subject, None for any other subject."""
    subject = unfold(subject)
    cover = parse_cover_letter(subject)
    if cover is not None:
        return 0, cover.total, cover.version, cover.title
    patch = parse_in_series_patch_subject(subject)
    if patch is None:
        return None
    version = _bracket_version(patch_bracket(subject)[0])
    return patch.position, patch.total, version, patch.canonical_subject


def _repeats_parent(
    subject: str | None,
    author: str | None,
    parent_subject: str | None,
    parent_author: str | None,
    *,
    any_sender: bool = False,
) -> bool:
    """Whether `subject` repeats its thread parent's posting, from another
    address unless `any_sender`: the same position, patch total, version
    and title, both or neither a RESEND. Another total or a RESEND on one
    side only is a re-post of the series.

    Reads the parent's subject and sender, never its stored series
    columns, so the answer does not depend on which of the two was
    stored first, and a reply to such a reply is caught too."""
    posting, parent = _posting(subject), _posting(parent_subject)
    if (
        posting is None
        or parent is None
        or posting[:3] != parent[:3]
        or posting_flags(parent_subject, parent[2])[1]
        != posting_flags(subject, posting[2])[1]
    ):
        return False
    address, title = _series_identity(posting[3], author)
    parent_address, parent_title = _series_identity(parent[3], parent_author)
    return title == parent_title and (any_sender or address != parent_address)


def reply_to_cover(
    subject: str | None,
    author: str | None,
    parent_subject: str | None,
    parent_author: str | None,
) -> bool:
    """Whether a cover-shaped `subject` is really a reply to its thread
    parent (#820), by `_repeats_parent`. Reviewers on some lists answer
    with the cover's subject as is, without "Re:". A reply from the
    cover's own address is left a cover: telling it from a re-post of
    the series needs the patches actually posted, which nothing here
    can see."""
    return parse_cover_letter(unfold(subject)) is not None and _repeats_parent(
        subject, author, parent_subject, parent_author
    )


def reply_to_patch(
    subject: str | None,
    author: str | None,
    parent_subject: str | None,
    parent_author: str | None,
    touches_files: bool,
) -> bool:
    """Whether an in-series `subject` is really a reply to its thread
    parent (#837), by `_repeats_parent`, and carries no diff
    (`touches_files` is whether `extract_touched_paths` found a path).
    Whoever sends it (#860): the diff, not the sender, tells a reply
    from a re-post. About 98% of such rows are replies (49 of 50
    sampled, production 2026-10-24); a diff marks a re-post of the
    patch. A re-post that attaches its patch, or a reply that quotes a
    diff unquoted, is misread either way."""
    return (
        not touches_files
        and parse_in_series_patch_subject(unfold(subject)) is not None
        and _repeats_parent(
            subject, author, parent_subject, parent_author, any_sender=True
        )
    )


# Parents a patch may climb to find its cover. Measured on production
# 2026-10-24 (read-only): the deepest chain is 99 hops, 95 among rows
# only this walk links, and 122 rows sit deeper than 64.
PATCH_WALK_HOPS = 128


def cover_of_patch(thread_parent, parent_of, posting):
    """The cover an in-series patch answers, or None. `posting` is the
    patch's `_posting`, `parent_of(message_id)` a row with `message_id`,
    `subject`, `author` and `thread_parent`, or None.

    Follows parents through in-series patches with the patch's total and
    version (`git send-email --chain-reply-to` threads each patch under
    the one before) to the first cover. A patch of another total or
    version is a re-post answering the old one, so the walk stops there.
    The cover itself is not compared: the cover a patch answers is its
    own, as it always was. Reads subjects, never stored series columns,
    so the answer does not depend on which rows were linked first."""
    seen = set()
    for _ in range(PATCH_WALK_HOPS):
        if thread_parent is None or thread_parent in seen:
            return None
        seen.add(thread_parent)
        row = parent_of(thread_parent)
        parent = None if row is None else _posting(row.subject)
        if parent is None:
            return None
        if parent[0] == 0:
            return row
        if parent[1:3] != posting[1:3]:
            return None
        thread_parent = row.thread_parent
    return None


def in_series_link(subject, thread_parent, parent_of) -> tuple[str, str] | None:
    """`(patch_series_key, patch_series_version)` for an in-series patch:
    its cover's (`cover_of_patch`), or None. A cover that is a reply to
    its own parent has no series (#820), so its patches have none."""
    posting = _posting(subject)
    if posting is None or posting[0] == 0:
        return None
    cover = cover_of_patch(thread_parent, parent_of, posting)
    if cover is None:
        return None
    above = parent_of(cover.thread_parent) if cover.thread_parent else None
    if above is not None and reply_to_cover(
        cover.subject, cover.author, above.subject, above.author
    ):
        return None
    parsed = parse_cover_letter(unfold(cover.subject))
    return series_key(parsed.title, cover.author), parsed.version


def _parent(session, thread_parent):
    """The thread parent's message id, subject, author and own parent, by
    one select on the indexed message id, or None."""
    if not thread_parent:
        return None
    return session.execute(
        select(
            Article.message_id,
            Article.subject,
            Article.author,
            Article.thread_parent,
        ).where(Article.message_id == thread_parent)
    ).one_or_none()


def _reply_to_cover_in_db(session, subject, author, thread_parent) -> bool:
    """`reply_to_cover` for a stored article."""
    parent = _parent(session, thread_parent)
    return parent is not None and reply_to_cover(
        subject, author, parent.subject, parent.author
    )


def _reply_to_patch_in_db(session, article, subject) -> bool:
    """`reply_to_patch` for a stored article. Its diff is whether
    `article_files` holds a row for it: ingest writes those rows from
    the same `extract_touched_paths` it decides by, and
    `backfill-article-files` fills them for history, so both paths
    agree once that backfill is complete."""
    parent = _parent(session, article.thread_parent)
    if parent is None:
        return False
    touches_files = has_article_files(session, article.id)
    return reply_to_patch(
        subject, article.author, parent.subject, parent.author, touches_files
    )


class BackfillResult(BackfillCounters):
    """Outcome counters for `backfill_patch_series`."""

    examined: int = 0
    indexed: int = 0  # cover letter detected, key + version + pos=0 written
    in_series_indexed: int = 0  # in-series patch linked to its cover (`in_series_link`)
    in_series_orphan: int = 0  # in-series patch, position set, cover not in DB
    not_cover: int = 0  # non-series-shaped subject (prose, replies, solo [PATCH])
    reply_cover: int = 0  # cover-shaped reply to a cover, series cleared (#820)
    reply_patch: int = 0  # patch-shaped reply to that patch, series cleared (#837)
    skipped: int = 0  # already fully resolved (idempotent re-run)
    subjects_unfolded: int = 0  # stored folded subject rewritten (#710)


def _process_one(session, article, reprocess: bool) -> tuple[str, object]:
    """Per-article work for `backfill_patch_series`. Returns
    `(bucket, pending_payload)`; `pending_payload` is None when the
    row's series columns and subject would not change, so no UPDATE
    fires and its key is not re-chained (#824). Otherwise it's a
    `_PatchSeriesPending` instance.

    Idempotency: an article is "fully resolved" when its
    `patch_series_position` is set AND (it's a cover with key, OR
    it's an in-series patch with key). An in-series patch with
    position but NULL key is an orphan that we try to re-link on
    every run, the cover may have arrived since the prior pass.

    The return-type annotation is `tuple[str, object]` because
    `_PatchSeriesPending` is imported lazily inside the function;
    ruff F821 would flag the precise annotation as undefined at
    module scope.
    """
    from mimir._pending_backfill import _PatchSeriesPending

    # Subjects stored before ingest unfolded them (#710) are parsed
    # unfolded and written back, in every mode, including rows that are
    # otherwise skipped.
    subject = unfold(article.subject)
    rewrite = subject if subject != article.subject else None
    stored = (
        article.patch_series_key,
        article.patch_series_version,
        article.patch_series_position,
    )

    def bucket(name: str):
        return (name, "subjects_unfolded") if rewrite is not None else name

    def pending(key, version, position) -> _PatchSeriesPending | None:
        # Correctness does not need this skip, since every payload's keys
        # are re-chained; cost does: a reprocess writes and re-chains only
        # the rows it changes, not every row it walks.
        if rewrite is None and (key, version, position) == stored:
            return None
        return _PatchSeriesPending(
            article_id=article.id,
            old_key=article.patch_series_key,
            patch_series_key=key,
            patch_series_version=version,
            patch_series_position=position,
            subject=rewrite,
        )

    is_fully_resolved = article.patch_series_position is not None and (
        (article.patch_series_position == 0 and article.patch_series_key is not None)
        or (article.patch_series_position > 0 and article.patch_series_key is not None)
    )
    if is_fully_resolved and not reprocess:
        return bucket("skipped"), pending(*stored)

    cover = parse_cover_letter(subject)
    if cover is not None and _reply_to_cover_in_db(
        session, subject, article.author, article.thread_parent
    ):
        # An already cleared reply is never "fully resolved", so every
        # plain run meets it again, and writes nothing unless the
        # subject needs it.
        return bucket("reply_cover"), pending(None, None, None)
    if cover is not None:
        return bucket("indexed"), pending(
            series_key(cover.title, article.author), cover.version, 0
        )

    in_series = parse_in_series_patch_subject(subject)
    if in_series is not None and _reply_to_patch_in_db(session, article, subject):
        # Like a reply cover: never "fully resolved", so re-checked by
        # every plain run, and its old key goes to the re-chain (#824).
        return bucket("reply_patch"), pending(None, None, None)
    if in_series is not None:
        link = in_series_link(
            subject, article.thread_parent, lambda mid: _parent(session, mid)
        )
        if link is not None:
            key, version = link
            return bucket("in_series_indexed"), pending(
                key, version, in_series.position
            )
        # Orphan: clear key/version on reprocess; keep position.
        kept = (None, None) if reprocess else stored[:2]
        return bucket("in_series_orphan"), pending(*kept, in_series.position)

    # Neither cover nor in-series. On reprocess, clear any prior series row.
    if reprocess:
        return bucket("not_cover"), pending(None, None, None)
    return bucket("not_cover"), pending(*stored)


def backfill_patch_series(
    limit: int | None = None,
    reprocess: bool = False,
    progress: Callable[[BackfillResult], None] | None = None,
    *,
    max_seconds: float | None = None,
    run: Run | None = None,
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

    `max_seconds` + `run` enable broker-side cooperative
    scheduling (Phase 2.2); see `mimir.patches.backfill_article_files`
    for the chunk/resume contract. Direct callers leave both None.
    """
    from mimir._pending_backfill import _submit_patch_series_batch

    result = BackfillResult()
    result.partial = walk_articles(
        result,
        _process_one,
        _submit_patch_series_batch,
        limit=limit,
        reprocess=reprocess,
        progress=progress,
        preload_lists=False,
        label="backfill_patch_series",
        max_seconds=max_seconds,
        run=run,
    )
    return result


__all__ = [
    "BackfillResult",
    "CoverLetter",
    "backfill_patch_series",
    "parse_cover_letter",
    "reply_to_cover",
    "reply_to_patch",
    "series_key",
]
