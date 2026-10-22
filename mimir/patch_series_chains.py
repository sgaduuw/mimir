"""Split a key's cover letters into the series a sender posted (#816).

`series_key` hashes author and cover title, so a sender who reuses a
generic title ("Netfilter updates for net-next") puts years of unrelated
postings on one key. `chain_series` walks one key's covers in posting
order and cuts them into chains; the first cover of every chain but the
key's first becomes the head that tells its series apart
(`patch_series_head_id`, read through `mimir.patch_series.same_series`).

`backfill_series_chains` applies it to the whole archive through the
broker, as a chunked, resumable backfill.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from mimir._backfill import BackfillCounters, walk_articles
from mimir.backfill_runs import Run
from mimir.models import Article
from mimir.parser import unfold
from mimir.patch_revisions import parse_in_series_patch_subject
from mimir.patch_series import chain_version_rank, posting_flags

# A cover that no rule joins to the chain starts a new one only when
# fewer than this share of its patch titles (or of the chain's recent
# ones, whichever side has fewer) match. Measured on production
# 2026-10-21 (3.24.0): at 0.5, a resend whose patch titles were
# reworded or reordered still joins, while two unrelated postings split.
# 0.34 joins more and is kept as the alternative if a sample check of
# the splits finds unrelated postings merged.
SPLIT_BELOW_MATCHED_SHARE = 0.5

# Two patch titles are the same patch when their word sets overlap this
# much: a resend adds or drops a "dt-bindings:" prefix, fixes a case.
_SAME_TITLE_JACCARD = 0.6

# The same version posted again within this many days is a resend of it.
_SAME_VERSION_DAYS = 1

_WORD_RE = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class Cover:
    """One cover letter, as `chain_series` reads it. `patch_titles` are
    the titles of the in-series patches posted under it (the part after
    the bracket)."""

    id: int
    message_id: str
    date: datetime | None
    version: str | None
    subject: str | None
    patch_titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Posting:
    cover: Cover
    rank: tuple[int, int]
    resend: bool
    title_words: tuple[frozenset[str], ...]


def _posting(cover: Cover) -> _Posting:
    is_rfc, resend = posting_flags(cover.subject, cover.version)
    words = (frozenset(_WORD_RE.findall(t.lower())) for t in cover.patch_titles)
    return _Posting(
        cover,
        chain_version_rank(cover.version, is_rfc),
        resend,
        tuple(w for w in words if w),
    )


def _matched_share(a: Sequence[frozenset[str]], b: Sequence[frozenset[str]]) -> float:
    """Containment: the fraction of the smaller list of titles that has
    a match in the other. Not the Jaccard of the lists, which a long
    series resent with a few patches dropped would fail."""
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    hits = sum(
        any(len(x & y) / len(x | y) >= _SAME_TITLE_JACCARD for y in big) for x in small
    )
    return hits / len(small)


def _joins(chain: Sequence[_Posting], new: _Posting) -> bool:
    latest = chain[-1]
    top = max(p.rank for p in chain)
    if new.rank > top or new.resend:
        return True
    if new.rank == top:
        a, b = latest.cover.date, new.cover.date
        if (
            a is None
            or b is None
            or (b - a).total_seconds() <= _SAME_VERSION_DAYS * 86400
        ):
            return True
    # Nothing to compare: a cover whose patches never arrived, or a
    # series posted without them, is not evidence of a new series.
    mine = new.title_words
    theirs = [w for p in chain[-2:] for w in p.title_words]
    if not mine or not theirs:
        return True
    return _matched_share(mine, theirs) >= SPLIT_BELOW_MATCHED_SHARE


def chain_series(covers: Sequence[Cover]) -> dict[int, int | None]:
    """The head of each cover of one key, by article id: None for the
    chain holding the key's oldest cover, otherwise the id of the
    chain's first cover.

    Covers are taken in `(date, message_id)` order, never by id, so the
    result depends on the set of covers and not on the order they were
    read or ingested. A cover joins the current chain when it is a
    version up (an RFC ranks below any other version), an explicit
    RESEND, or the same version as the latest cover within a day. Failing
    those it joins unless both it and the chain's last two postings have
    patch titles and fewer than `SPLIT_BELOW_MATCHED_SHARE` of them match.
    """
    ordered = sorted(covers, key=lambda c: (c.date is not None, c.date, c.message_id))
    chains: list[list[_Posting]] = []
    for cover in ordered:
        posting = _posting(cover)
        if chains and _joins(chains[-1], posting):
            chains[-1].append(posting)
        else:
            chains.append([posting])
    return {
        p.cover.id: None if i == 0 else chain[0].cover.id
        for i, chain in enumerate(chains)
        for p in chain
    }


class BackfillResult(BackfillCounters):
    """Outcome counters for `backfill_series_chains`."""

    examined: int = 0
    # keys_chained and keys_split count per chunk: a key met again by a later
    # chunk of the run is counted again, so a session total can exceed the
    # number of keys.
    keys_chained: int = 0  # keys chained afresh (more than one cover, or stale heads)
    keys_split: int = 0  # of those, keys that came out as more than one series
    heads_written: int = 0  # cover and patch rows whose head changed
    skipped: int = 0  # not a cover, a key of one cover, or a key already done


def _load_key(
    session, key: str
) -> tuple[list[Cover], dict[int, int | None], list[tuple]]:
    """A key's covers, their stored heads, and its in-series patches as
    `(id, cover message id, stored head)`. The one place that reads a
    whole key: it must see every series on it."""
    cover_rows = session.execute(
        select(
            Article.id,
            Article.message_id,
            Article.date,
            Article.patch_series_version,
            Article.subject,
            Article.patch_series_head_id,
        ).where(
            Article.patch_series_key == key,
            func.coalesce(Article.patch_series_position, 0) == 0,
        )
    ).all()
    patch = aliased(Article)
    patch_rows = session.execute(
        select(patch.id, patch.thread_parent, patch.subject, patch.patch_series_head_id)
        .join(Article, patch.thread_parent == Article.message_id)
        .where(
            Article.patch_series_key == key,
            func.coalesce(Article.patch_series_position, 0) == 0,
            patch.patch_series_key == key,
            patch.patch_series_position > 0,
        )
    ).all()
    titles: dict[str, list[str]] = {}
    patches = []
    for pid, parent, subject, head in patch_rows:
        parsed = parse_in_series_patch_subject(unfold(subject or ""))
        if parsed is not None:
            titles.setdefault(parent, []).append(parsed.canonical_subject)
        patches.append((pid, parent, head))
    covers = [
        Cover(
            id=r.id,
            message_id=r.message_id,
            date=r.date,
            version=r.patch_series_version,
            subject=r.subject,
            patch_titles=tuple(titles.get(r.message_id, ())),
        )
        for r in cover_rows
    ]
    return covers, {r.id: r.patch_series_head_id for r in cover_rows}, patches


def backfill_series_chains(
    limit: int | None = None,
    reprocess: bool = False,
    progress: Callable[[BackfillResult], None] | None = None,
    *,
    max_seconds: float | None = None,
    run: Run | None = None,
) -> BackfillResult:
    """Chain every key that has more than one cover letter, or a head to
    clear, and write the heads that changed, on the covers and on their
    in-series patches.

    Walks articles newest-first like the other backfills. A cover's key
    is chained whole, from the database, the first time the walk meets
    one of its covers in a chunk, and remembered so its other covers
    cost nothing: `backfill_runs.cursor` is an article id, so a key
    cannot be a unit of the cursor. A key chained again by a later chunk
    writes nothing, since only changed heads are written. A key's rows
    go in one batch, so a failed batch leaves no half-chained key.

    `reprocess` changes nothing here: every run recomputes whole keys.
    The parameter is the shape the broker's backfill handlers share.
    """
    from mimir._pending_backfill import _SeriesHeadsPending, _submit_series_heads_batch

    result = BackfillResult()
    done: set[str] = set()

    def process_one(session, article, _reprocess):
        key = article.patch_series_key
        if key is None or (article.patch_series_position or 0) != 0 or key in done:
            return "skipped", None
        done.add(key)
        covers, stored, patches = _load_key(session, key)
        # A key of one cover has nothing to split, but a head left from
        # when it had more must still be cleared: a lone cover chains to
        # None.
        if len(covers) < 2 and all(
            h is None for h in (*stored.values(), *(p[2] for p in patches))
        ):
            return "skipped", None
        heads = chain_series(covers)
        cover_of = {c.message_id: c.id for c in covers}
        updates = [(i, h) for i, h in heads.items() if stored[i] != h]
        updates += [
            (pid, heads[cover_of[parent]])
            for pid, parent, head in patches
            # A parent missing from `cover_of` committed between the two
            # reads of `_load_key`; the next run sees it.
            if parent in cover_of and head != heads[cover_of[parent]]
        ]
        result.heads_written += len(updates)
        bucket = "keys_chained"
        if len(set(heads.values())) > 1:
            bucket = ("keys_chained", "keys_split")
        return bucket, _SeriesHeadsPending(updates) if updates else None

    result.partial = walk_articles(
        result,
        process_one,
        _submit_series_heads_batch,
        limit=limit,
        reprocess=reprocess,
        progress=progress,
        preload_lists=False,
        label="backfill_series_chains",
        max_seconds=max_seconds,
        run=run,
    )
    return result
