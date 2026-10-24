"""Split a key's cover letters into the series a sender posted (#816).

`series_key` hashes author and cover title, so a sender who reuses a
generic title ("Netfilter updates for net-next") puts years of unrelated
postings on one key. `chain_series` walks one key's covers in posting
order and cuts them into chains; the first cover of every chain but the
key's first becomes the head that tells its series apart
(`patch_series_head_id`, read through `mimir.patch_series.same_series`).

`head_updates` turns one key into the rows whose head must change, and
the rows whose version must: the re-chain is the last writer of a
posting's version (`posting_version`, #870). `rechain_keys` applies both
to keys on the writer's connection, inside the transaction of whatever
touched them: an ingest batch, a replay, or a batch of
`backfill_series_chains`, which walks the whole archive through the
broker as a chunked, resumable backfill.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from mimir._backfill import BackfillCounters, walk_articles
from mimir.backfill_runs import Run
from mimir.models import Article
from mimir.patch_series import (
    _parent,
    chain_version_rank,
    cover_of_patch,
    posting_flags,
    posting_version,
)
from mimir.patch_series import _posting as _subject_posting

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
    top = max(p.rank for p in chain)
    if new.rank > top or new.resend:
        return True
    if new.rank == top:
        a, b = chain[-1].cover.date, new.cover.date
        if a is None or b is None or b - a <= timedelta(days=_SAME_VERSION_DAYS):
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
    # keys chained afresh (more than one cover, or stale heads or versions)
    keys_chained: int = 0
    keys_split: int = 0  # of those, keys that came out as more than one series
    heads_planned: int = 0  # cover and patch rows the read found to change
    versions_planned: int = 0  # cover and patch rows whose version changes (#870)
    skipped: int = 0  # not a cover, a key of one cover, or a key already done


def _load_key(
    session, key: str
) -> tuple[list[Cover], dict[int, int | None], list[tuple], list[tuple[int, str]]]:
    """A key's covers, their stored heads, its in-series patches as
    `(id, cover message id, stored head, stored version)`, and the
    `(id, version)` of the cover and patch rows whose stored version
    differs from their posting's (`posting_version`, #870). The one place
    that reads a whole key: it must see every series on it. Each `Cover`
    carries its posting's version, so `chain_series` ranks on it.

    A patch belongs to the cover `cover_of_patch` finds for it, the walk
    that linked it (#840), so a patch answering the patch before it is
    titled and headed with its cover. The walk reads the key's own rows
    first and the database for the rest: a patch can answer a reply that
    #837 cleared, which carries no key."""
    cover_rows = session.execute(
        select(
            Article.id,
            Article.message_id,
            Article.date,
            Article.patch_series_version,
            Article.subject,
            Article.author,
            Article.thread_parent,
            Article.patch_series_head_id,
        ).where(
            Article.patch_series_key == key,
            func.coalesce(Article.patch_series_position, 0) == 0,
        )
    ).all()
    patch_rows = session.execute(
        select(
            Article.id,
            Article.message_id,
            Article.subject,
            Article.author,
            Article.date,
            Article.thread_parent,
            Article.patch_series_version,
            Article.patch_series_head_id,
        ).where(
            Article.patch_series_key == key,
            Article.patch_series_position > 0,
        )
    ).all()
    rows = {r.message_id: r for r in (*cover_rows, *patch_rows)}

    def parent_of(message_id):
        return rows.get(message_id) or _parent(session, message_id)

    titles: dict[str, list[str]] = {}
    subjects: dict[str, list[str]] = {}
    patches = []
    for row in patch_rows:
        posting = _subject_posting(row.subject)
        if posting is None or posting[0] == 0:
            continue
        cover = cover_of_patch(
            row.thread_parent, parent_of, posting, row.subject, row.date
        )
        if cover is None:
            continue
        titles.setdefault(cover.message_id, []).append(posting[3])
        subjects.setdefault(cover.message_id, []).append(row.subject)
        patches.append(
            (
                row.id,
                cover.message_id,
                row.patch_series_head_id,
                row.patch_series_version,
            )
        )
    version_of = {
        r.message_id: posting_version(
            r.subject, subjects.get(r.message_id, ()), r.patch_series_version
        )
        for r in cover_rows
    }
    covers = [
        Cover(
            id=r.id,
            message_id=r.message_id,
            date=r.date,
            version=version_of[r.message_id],
            subject=r.subject,
            patch_titles=tuple(titles.get(r.message_id, ())),
        )
        for r in cover_rows
    ]
    versions = [
        (r.id, version_of[r.message_id])
        for r in cover_rows
        if r.patch_series_version != version_of[r.message_id]
    ]
    # A cover missing from `version_of` is not one of this key's covers,
    # as in `head_updates`.
    versions += [
        (pid, version_of[parent])
        for pid, parent, _head, version in patches
        if parent in version_of and version != version_of[parent]
    ]
    heads = {r.id: r.patch_series_head_id for r in cover_rows}
    return covers, heads, patches, versions


def head_updates(
    session, key: str
) -> tuple[list[tuple[int, int | None]], list[tuple[int, str]], bool] | None:
    """`(updates, versions, split)` for one key, read whole from what
    `session` (a Session or the writer's Connection) sees: the
    `(article id, head)` pairs of covers and in-series patches whose
    stored head differs from `chain_series`, the `(article id, version)`
    pairs whose stored version differs from their posting's, and whether
    the key holds more than one series. None when there is nothing to
    chain or write."""
    covers, stored, patches, versions = _load_key(session, key)
    heads = chain_series(covers)
    cover_of = {c.message_id: c.id for c in covers}
    updates = [(i, h) for i, h in heads.items() if stored[i] != h]
    updates += [
        (pid, heads[cover_of[parent]])
        for pid, parent, head, _version in patches
        # A cover missing from `cover_of` is not one of this key's covers:
        # a reply #820 cleared, a stale key, or a cover committed between
        # the two reads of `_load_key`, which the next run sees.
        if parent in cover_of and head != heads[cover_of[parent]]
    ]
    # A key of one cover has nothing to split, but a head left from when
    # it had more is still written: a lone cover chains to None. So is a
    # version, since a lone posting can have a typo.
    if len(covers) < 2 and not updates and not versions:
        return None
    return updates, versions, len(set(heads.values())) > 1


def rechain_keys(conn, keys) -> int:
    """Re-chain each of `keys` from the state `conn` sees and write the
    heads and versions that changed; returns the rows written.

    It writes a posting's version last, after the subject's own that
    ingest, replay and `backfill-patch-series` store, so the patches'
    version wins over a cover's typo in the same transaction (#870).

    Called inside the WriteOps of an ingest batch, a replay and a
    backfill batch, after any inserts. A new cover and the heads it
    moves commit or roll back together, so no reader sees the cover in
    the wrong series and a crash leaves no key to repair; and a backfill
    planned from an older read cannot undo an ingest batch's heads.

    A cover's key covers its patches too, since an in-series patch
    carries its cover's key, and a patch arriving changes its cover's
    titles, which the rule reads. Whole keys, never the new rows alone:
    an older posting arriving late moves every later chain of its key.
    """
    written = 0
    for key in sorted(keys):
        heads, versions, _split = head_updates(conn, key) or ((), (), False)
        for article_id, head in heads:
            conn.execute(
                update(Article)
                .where(Article.id == article_id)
                .values(patch_series_head_id=head)
            )
            written += 1
        for article_id, version in versions:
            conn.execute(
                update(Article)
                .where(Article.id == article_id)
                .values(patch_series_version=version)
            )
            written += 1
    return written


def backfill_series_chains(
    limit: int | None = None,
    reprocess: bool = False,
    progress: Callable[[BackfillResult], None] | None = None,
    *,
    max_seconds: float | None = None,
    run: Run | None = None,
) -> BackfillResult:
    """Chain every key that has more than one cover letter, or a head to
    clear, or a version to change, and write the heads and versions that
    changed, on the covers and on their in-series patches.

    Walks articles newest-first like the other backfills. A cover's key
    is chained whole, from the database, the first time the walk meets
    one of its covers in a chunk, and remembered so its other covers
    cost nothing: `backfill_runs.cursor` is an article id, so a key
    cannot be a unit of the cursor. A key chained again by a later chunk
    writes nothing, since only changed heads are written. The read only
    decides which keys to send and feeds the counters; the batch
    re-chains each key on the writer, so its rows are written from the
    state at write time and a failed batch leaves no half-chained key.

    `reprocess` changes nothing here: every run recomputes whole keys.
    The parameter is the shape the broker's backfill handlers share.
    """
    from mimir._pending_backfill import _SeriesKeyPending, _submit_series_heads_batch

    result = BackfillResult()
    done: set[str] = set()

    def process_one(session, article, _reprocess):
        key = article.patch_series_key
        if key is None or (article.patch_series_position or 0) != 0 or key in done:
            return "skipped", None
        done.add(key)
        changed = head_updates(session, key)
        if changed is None:
            return "skipped", None
        updates, versions, split = changed
        result.heads_planned += len(updates)
        result.versions_planned += len(versions)
        bucket = ("keys_chained", "keys_split") if split else "keys_chained"
        return bucket, _SeriesKeyPending(key) if updates or versions else None

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
