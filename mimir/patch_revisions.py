"""Resolve the in-series patches of a cover letter, and match
positions across revisions of the same series.

Slice 1 of patch-series detection (`mimir/patch_series.py`) only
writes `patch_series_key` to *cover letters*. The in-series patches
(`[PATCH v2 1/3]`, `[PATCH v2 2/3]`, ...) have NULL `patch_series_key`
in the DB today, so the inter-revision diff route (#210) can't
reach them by indexed lookup. This module bridges that gap at
request time by walking each cover letter's thread children and
parsing `[PATCH ... N/M]` from their subjects.

The matching heuristic (`match_revision_position`) is the
load-bearing piece: when v1 has `[PATCH 3/5] foo: do bar` and v2
has `[PATCH v2 3/4] foo: do bar` (position shifted because a
patch was dropped), it pairs them by canonical-subject equality
first, file-overlap fallback second, and reports "ambiguous" or
"no match" rather than guessing wrong. The cost of guessing on a
review surface is worse than the cost of a 404.

Companion #212 will promote this resolver into a persisted
`patch_series_position` column once the heuristic has been
validated against prod data; this module is the prototype that
informs that schema decision.
"""

import difflib
import re
from dataclasses import dataclass

from pygments import highlight
from pygments.formatters.html import HtmlFormatter
from pygments.lexers import DiffLexer
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from mimir import cache
from mimir.models import Article, ArticleList, Inbox


# `^[<inside>] <title>` matched only when the bracket starts the
# subject; matches the existing cover-letter parser's anchoring so
# `Re:` replies don't masquerade as in-series patches.
_BRACKET_RE = re.compile(r"^\[([^\]]+)\]\s*(.+)$")

# `N/M` shape inside the bracket. We accept `N >= 1` so the cover
# letter's `0/M` doesn't match here (the cover letter is detected
# upstream via `patch_series.parse_cover_letter`). The optional
# `0*` prefix tolerates zero-padded shapes like `01/27`, `09/27`
# produced by `git format-patch --numbered` on ≥10-patch series;
# the capture group still pulls the actual position (`1`, `9`).
_N_OF_M_RE = re.compile(r"\b0*([1-9]\d*)\s*/\s*(\d+)\b")

# The bracket must literally contain `PATCH`. `[GIT PULL]` and
# `[ANNOUNCE]` subjects can carry `N/M` shapes too (for entirely
# unrelated reasons); the PATCH token disambiguates.
_PATCH_TOKEN_RE = re.compile(r"\bPATCH\b")


@dataclass(frozen=True)
class ParsedPatchSubject:
    """One in-series patch subject parsed out of `[PATCH ... N/M] title`.

    `position` is 1-indexed, never 0 (cover letters are filtered upstream).
    `total` is the M of `N/M`; can differ across revisions of the same
    series when patches are added or dropped.
    `canonical_subject` is the part after the bracket; matching across
    revisions compares this (case-folded + whitespace-collapsed)."""

    position: int
    total: int
    canonical_subject: str


def parse_in_series_patch_subject(subject: str | None) -> ParsedPatchSubject | None:
    """Detect whether `subject` opens an in-series patch and return
    its parsed shape, or None for cover letters, single patches,
    replies, and every non-patch subject.

    Recognises:
        [PATCH 1/3] foo: do bar                 -> pos=1 total=3
        [PATCH v2 1/3] foo: do bar              -> pos=1 total=3
        [PATCH net 1/3] foo: do bar             -> pos=1 total=3
        [PATCH net-next v2 1/3] foo: do bar     -> pos=1 total=3
        [PATCH RFC v2 1/3] foo: do bar          -> pos=1 total=3

    Doesn't recognise:
        [PATCH 0/3] cover letter title          (cover letter; pos=0 filtered)
        [PATCH] solo patch                      (no N/M)
        [PATCH v2] solo patch                   (no N/M)
        Re: [PATCH v2 1/3] title                (bracket not at column 0)
        [GIT PULL 1/3] title                    (no PATCH token)
    """
    if not subject:
        return None
    m = _BRACKET_RE.match(subject.strip())
    if m is None:
        return None
    inside, title = m.group(1), m.group(2).strip()
    if not _PATCH_TOKEN_RE.search(inside):
        return None
    nom = _N_OF_M_RE.search(inside)
    if nom is None:
        return None
    try:
        position = int(nom.group(1))
        total = int(nom.group(2))
    except ValueError, IndexError:
        return None
    return ParsedPatchSubject(
        position=position,
        total=total,
        canonical_subject=title,
    )


@dataclass(frozen=True)
class InSeriesPatch:
    """One patch resolved from a cover letter's thread.

    `position` is 0 for the cover letter itself, 1+ for in-series
    patches. Ordering is by `position`; the cover letter sorts first.

    `canonical_subject` is the post-bracket title (cover letter's
    title for position 0). Used by the matcher when subjects vary
    between revisions.

    `touched_paths` is the set of `b/` paths from `article_files`
    (preloaded). Used by the matcher's file-overlap fallback when
    subjects don't line up across revisions. Empty for cover
    letters (they're prose, not patches)."""

    position: int
    article: Article
    canonical_subject: str
    touched_paths: frozenset[str]


def _normalize_subject(s: str) -> str:
    """Case-folded, whitespace-collapsed for cross-revision compare."""
    return " ".join(s.lower().split())


def resolve_series_patches(
    session: Session,
    inbox: Inbox,
    cover_article: Article,
) -> list[InSeriesPatch]:
    """Walk `cover_article`'s thread children (within `inbox`) and
    return the in-series patches found, with the cover letter at
    position 0.

    The walk uses a single SELECT on `Article.thread_parent =
    <cover.message_id>`, joined to `article_lists` for the inbox
    filter; we deliberately don't recurse beyond depth 1. In-series
    patches are conventionally posted as direct replies to the
    cover letter (`git send-email --thread`); replies to individual
    patches are review discussion, not other patches in the series.

    Returns a list ordered by position (cover at index 0, then
    1, 2, ...). Duplicate positions (same `N/M` reused) are kept
    as-is; the matcher decides what to do with them. Subject
    parsing failures (children that don't match `[PATCH ... N/M]`)
    are filtered out: those are review replies, not patches.

    `article_files` is preloaded via `selectinload` so the
    file-overlap fallback in the matcher doesn't trigger N+1
    lazy fetches.
    """
    cover_entry = InSeriesPatch(
        position=0,
        article=cover_article,
        canonical_subject=(cover_article.subject or "").strip(),
        touched_paths=frozenset(f.path for f in (cover_article.files or [])),
    )
    children = (
        session.execute(
            select(Article)
            .options(selectinload(Article.files))
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(
                Article.thread_parent == cover_article.message_id,
                ArticleList.inbox_id == inbox.id,
            )
        )
        .scalars()
        .all()
    )
    in_series: list[InSeriesPatch] = []
    for child in children:
        parsed = parse_in_series_patch_subject(child.subject)
        if parsed is None:
            continue
        in_series.append(
            InSeriesPatch(
                position=parsed.position,
                article=child,
                canonical_subject=parsed.canonical_subject,
                touched_paths=frozenset(f.path for f in (child.files or [])),
            )
        )
    in_series.sort(key=lambda p: p.position)
    return [cover_entry, *in_series]


# Distinct sentinel for "more than one plausible match"; the route
# distinguishes this from "no match" so the rendered 404 message
# can tell the reader why (dropped vs ambiguous).
AMBIGUOUS_MATCH = object()


def match_revision_position(
    v1_patches: list[InSeriesPatch],
    v2_patches: list[InSeriesPatch],
    pos: int,
) -> tuple[InSeriesPatch, InSeriesPatch] | None | object:
    """Find the patch in `v2_patches` corresponding to the patch
    at `pos` in `v1_patches`.

    Returns:
    - `(v1_match, v2_match)` on a confident match.
    - `None` when v1 has no patch at `pos`, or no v2 candidate
      plausibly corresponds.
    - `AMBIGUOUS_MATCH` when two or more v2 candidates plausibly
      correspond and can't be disambiguated by file overlap. The
      route turns this into a 404 with a "couldn't pick" message
      rather than guessing wrong on a review surface.

    Matching strategy (in order):
    1. Position 0 (cover): both lists have exactly one position-0
       entry; trivially matched.
    2. Canonical-subject equality (normalised case + whitespace).
       The modal case for unchanged-title patches.
    3. File-overlap fallback when subjects differ across revisions
       (a maintainer rewrote the commit subject). Pick the v2
       candidate with maximum touched-path intersection; require
       at least one path in common to count.

    Ties are NOT broken arbitrarily; we'd rather 404 than pick wrong.
    """
    v1_match = next((p for p in v1_patches if p.position == pos), None)
    if v1_match is None:
        return None
    if pos == 0:
        v2_cover = next((p for p in v2_patches if p.position == 0), None)
        return (v1_match, v2_cover) if v2_cover is not None else None

    v1_norm = _normalize_subject(v1_match.canonical_subject)
    v2_candidates = [p for p in v2_patches if p.position > 0]
    subject_matches = [
        p for p in v2_candidates if _normalize_subject(p.canonical_subject) == v1_norm
    ]
    if len(subject_matches) == 1:
        return (v1_match, subject_matches[0])
    if len(subject_matches) > 1:
        # Multiple v2 patches share v1's title (unusual but possible
        # after a rename/split). Disambiguate by file overlap.
        return _pick_by_file_overlap(v1_match, subject_matches)
    # No subject match: try file-overlap across all v2 in-series.
    return _pick_by_file_overlap(v1_match, v2_candidates)


def _pick_by_file_overlap(
    v1_match: InSeriesPatch,
    candidates: list[InSeriesPatch],
) -> tuple[InSeriesPatch, InSeriesPatch] | None | object:
    """Return the candidate with maximum touched-file overlap with
    `v1_match`. Requires at least one path in common; returns None
    when none overlap, `AMBIGUOUS_MATCH` when two or more tie at
    the same nonzero overlap count."""
    if not v1_match.touched_paths or not candidates:
        return None
    scored = [(len(v1_match.touched_paths & c.touched_paths), c) for c in candidates]
    scored = [(n, c) for n, c in scored if n > 0]
    if not scored:
        return None
    scored.sort(key=lambda nc: -nc[0])
    top_score = scored[0][0]
    top_candidates = [c for n, c in scored if n == top_score]
    if len(top_candidates) > 1:
        return AMBIGUOUS_MATCH
    return (v1_match, top_candidates[0])


@dataclass
class RevisionDiff:
    """One cached inter-revision diff. `diff_html` is the
    pre-rendered Pygments output ready to drop into a template;
    `is_identical` lets the template render `No changes between
    v1 and v2 of this patch` instead of an empty diff block.

    `from_url` / `to_url` are the canonical message-page URLs for
    each side, captured at compute time so the cached entry stays
    self-contained for the template (no second SQL query at
    template-render time to reconstruct date components for URL
    building).

    Dataclass (not pydantic) for cache-encoder compatibility,
    matching the project convention (see other `cache.register`
    consumers in `dashboard.py`, `subsystems_dashboard/`).
    """

    from_article_id: int
    to_article_id: int
    from_version: str
    to_version: str
    from_url: str
    to_url: str
    position: int
    diff_html: str
    is_identical: bool


cache.register("RevisionDiff", RevisionDiff)


# Pygments setup mirrors the inline-diff render in
# `mimir/rendering/diff.py`.
# Two-instance duplication is intentional: rendering.py's instances are
# underscore-prefixed module-locals (not part of its public surface),
# and adding a public helper there for one extra caller would be the
# premature-abstraction trap. If a third caller appears, promote.
_DIFF_LEXER = DiffLexer()
_DIFF_FORMATTER = HtmlFormatter(
    noclasses=False, nobackground=True, cssclass="highlight"
)


def compute_revision_diff(
    from_body: str | None,
    to_body: str | None,
    *,
    from_article_id: int,
    to_article_id: int,
    from_version: str,
    to_version: str,
    from_url: str,
    to_url: str,
    position: int,
) -> RevisionDiff:
    """Diff two full message bodies (commit message + patch hunks)
    and return a cache-ready `RevisionDiff`.

    Full-body input was the deliberate choice (issue #210 discussion):
    catching changes to the commit message (added Fixes: trailer,
    rewritten explanation) is as useful to reviewers as catching
    changes to the patch hunks themselves. The Pygments DiffLexer
    handles mixed prose + diff cleanly.

    None / empty bodies are normalised to empty strings; the diff
    will show "this whole side is new" or "this whole side was
    removed", which is the correct rendering when one revision's
    body is unavailable from the mirror.
    """
    a = (from_body or "").splitlines(keepends=False)
    b = (to_body or "").splitlines(keepends=False)
    is_identical = a == b
    if is_identical:
        diff_html = ""
    else:
        diff_lines = list(
            difflib.unified_diff(
                a,
                b,
                fromfile=f"v{from_version.lstrip('v')}",
                tofile=f"v{to_version.lstrip('v')}",
                lineterm="",
            )
        )
        diff_text = "\n".join(diff_lines)
        diff_html = highlight(diff_text, _DIFF_LEXER, _DIFF_FORMATTER)
    return RevisionDiff(
        from_article_id=from_article_id,
        to_article_id=to_article_id,
        from_version=from_version,
        to_version=to_version,
        from_url=from_url,
        to_url=to_url,
        position=position,
        diff_html=diff_html,
        is_identical=is_identical,
    )


__all__ = [
    "AMBIGUOUS_MATCH",
    "InSeriesPatch",
    "ParsedPatchSubject",
    "RevisionDiff",
    "compute_revision_diff",
    "match_revision_position",
    "parse_in_series_patch_subject",
    "resolve_series_patches",
]
