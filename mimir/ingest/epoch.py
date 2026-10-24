"""Core per-epoch ingest walk + the small helpers it shares with the
replay and backfill flows.

`ingest_epoch` is the hot path: walk a public-inbox epoch's git log
from the last-seen commit forward, parse messages (sequentially or via
a process pool), bucket each outcome (new / linked / dup_batch /
dup_db / failed), and persist a per-epoch resume cursor.

`_to_article_insert` builds a new article's row for `ingest_epoch` and
`.replay`; `IngestResult` and `DEFAULT_WORKERS` are imported more
widely. The Phase 3b write path (observation flush, list-address
promotion) lives as composite WriteOps in `._pending`, not here.
"""

import logging
import os
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from functools import partial
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Imported lazily at call-site to avoid a heavy circular import on the
    # hot ingest path; imported here only so type-checkers and ruff can
    # resolve the forward-reference string annotations.
    from mimir.broker.writes import WriterThread
    from mimir.ingest._pending import _ArticleInsert

from dulwich.repo import Repo
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from mimir.canonical import extract_list_addresses, pick_canonical_inbox_id
from mimir.config import settings
from mimir.datetime_utils import aware_utc
from mimir.models import (
    Article,
    ArticleList,
    Inbox,
    IngestState,
    ParseFailure,
)
from mimir.parser import ParsedArticle, normalize_subject, parse_message
from mimir.patch_revisions import parse_in_series_patch_subject
from mimir.patch_series import (
    in_series_link,
    parse_cover_letter,
    reply_to_cover,
    reply_to_patch,
    series_key,
)
from mimir.patches import extract_touched_paths
from mimir.trailers import extract_trailers

logger = logging.getLogger(__name__)

# Log a progress line every PROGRESS_EVERY rows during a backfill.
# Set to 1000 so a first-run full-mirror ingest (~6M rows on lkml)
# emits ~6k progress lines rather than 60k; -v steady-state ticks
# are short enough that one line per batch is fine without further
# subsampling.
PROGRESS_EVERY = 1000
COMMIT_EVERY = 500
# Time cap on the writer-lock-hold per commit batch (seconds). In
# broker mode (1.36.0+) the broker's long worker and cache worker
# share the SQLite writer lock; while the long worker holds it for a
# multi-second commit, every queued cache_set blocks. A page render
# with N cached surfaces stacks N x (commit hold) of wait time and
# trips the 30 s gateway timeout on a cold front page. Capping
# per-commit hold at 500 ms keeps that stack under ~5 s comfortably.
#
# Phase 3b replaces COMMIT_EVERY_SECONDS with settings.ingest_batch_flush_seconds.
# See _claude/plans/2026-05-30-broker-two-pool-phase-3b-ingest.md for context.
# The value was previously a module constant; env-tunable default remains 0.5.
# (The constant is deleted below per the step plan; callers read from settings.)
DEFAULT_WORKERS = os.cpu_count() or 1
PARSE_CHUNKSIZE = 50


# Auto-promotion of `Inbox.list_address`: an inbox needs at least
# MIN_PROMOTE_OBSERVATIONS messages observed before the modal address
# can be trusted, AND the modal address must account for at least
# PROMOTE_DOMINANCE of the top-two combined to count as a clear winner.
# Tuned conservative, false promotion would silently misroute canonical
# resolution for every cross-posted article involving this inbox.
MIN_PROMOTE_OBSERVATIONS = 50
PROMOTE_DOMINANCE = 0.7


class IngestResult(BaseModel):
    """Per-epoch outcome counters. Every walked commit lands in
    exactly one of: new, linked, dup_batch, dup_db, failed."""

    epoch: str
    new: int = 0
    # Article existed in another inbox (cross-post): added a new
    # ArticleList row pointing at this inbox.
    linked: int = 0
    # Same Message-ID seen earlier in the current uncommitted batch.
    dup_batch: int = 0
    # Article already in DB and already linked to this inbox, usually
    # a re-walk (rewound IngestState, reindex without --from-scratch),
    # or a public-inbox archive that committed the same `m` blob twice.
    dup_db: int = 0
    failed: int = 0
    last_commit_sha: str | None = None
    # Message IDs of articles created (`new` bucket only, cross-post
    # `linked` rows don't produce a new public URL). Consumed by the
    # `update` scheduler tick to feed IndexNow push notifications.
    # Bounded by `result.new` per epoch; steady-state lkml ticks are
    # in the dozens-to-hundreds range. `reindex --from-scratch`-style
    # operations can balloon this, but those code paths don't call
    # the IndexNow notifier, the cost there is just a list of
    # short strings the caller discards.
    new_message_ids: list[str] = []


def _walk_epoch(
    repo_path: Path,
    since_sha: str | None,
) -> Iterator[tuple[str, datetime, bytes]]:
    """Yield (commit_sha, commit_time_utc, raw_message_bytes) per epoch commit.

    Repo is context-managed so its pack-file mmaps release when the
    generator exhausts OR when the consumer abandons it (Python calls
    `generator.close()` which raises `GeneratorExit` into the
    suspended frame; the `with` block's `__exit__` runs then). Without
    `with` the Repo would survive until GC, holding mmaps in VmData
    across concurrent multi-inbox ingests. Same shape as the v3.1.2
    `mainline.py` fix.
    """
    with Repo(str(repo_path)) as repo:
        head = repo.head()
        exclude: list[bytes] = []
        if since_sha:
            try:
                repo[since_sha.encode()]
                exclude = [since_sha.encode()]
            except KeyError:
                exclude = []

        walker = repo.get_walker(include=[head], exclude=exclude, reverse=True)
        for entry in walker:
            commit = entry.commit
            tree = repo[commit.tree]
            try:
                _mode, blob_sha = tree[b"m"]
            except KeyError:
                continue
            blob = repo[blob_sha]
            commit_time = datetime.fromtimestamp(commit.commit_time, UTC)
            yield commit.id.decode(), commit_time, blob.data


def _parse_pair(
    item: tuple[str, datetime, bytes],
) -> tuple[str, datetime, ParsedArticle | Exception]:
    """Worker entry point: parse one message, return either the parsed article
    or the exception object, exceptions can't propagate out of pool.map cleanly
    without tearing down the whole pool. The commit_time is threaded through
    so the main process can use it as a fallback for bogus message Dates."""
    commit_sha, commit_time, raw = item
    try:
        return commit_sha, commit_time, parse_message(raw)
    except Exception as exc:
        return commit_sha, commit_time, exc


def _parse_iter(
    items: Iterable[tuple[str, datetime, bytes]],
    workers: int,
) -> Iterator[tuple[str, datetime, ParsedArticle | Exception]]:
    """Yield (commit_sha, commit_time, parsed | exception) tuples in input order."""
    if workers <= 1:
        for item in items:
            yield _parse_pair(item)
        return

    with ProcessPoolExecutor(max_workers=workers) as pool:
        yield from pool.map(_parse_pair, items, chunksize=PARSE_CHUNKSIZE)


def _thread_parent(session: Session, message_id: str, pending_articles: list | None):
    """The parent article's message id, subject, author and own parent:
    from the current batch first, which is not committed yet, else one
    select on the indexed message id. None when neither holds it."""
    for art in pending_articles or ():
        if art.message_id == message_id:
            return art
    return session.execute(
        select(
            Article.message_id,
            Article.subject,
            Article.author,
            Article.thread_parent,
        ).where(Article.message_id == message_id)
    ).one_or_none()


def _to_article_insert(
    parsed: ParsedArticle,
    inbox_id: int,
    epoch: str,
    commit_sha: str,
    date: datetime,
    canonical_inbox_id: int | None = None,
    session: Session | None = None,
    pending_articles: list | None = None,
) -> _ArticleInsert:
    """Build an `_ArticleInsert` record for a new article without touching
    the ORM session (except for the optional thread-parent lookup).

    The one place a new article's `thread_parent`, `subject_normalized`,
    patch-series identity, touched paths and trailers are derived, for
    `ingest_epoch` and `.replay` alike (#755).

    `session` is required only for the thread-parent lookup that links
    an in-series patch to its cover and tells a reply from a cover;
    replay passes None and skips it. `pending_articles` is the current
    batch's list of already-accumulated `_ArticleInsert` objects so a
    parent in the same batch can be found without a DB query.
    """
    from mimir.ingest._pending import _ArticleInsert

    thread_parent = parsed.in_reply_to or (
        parsed.references[-1] if parsed.references else None
    )
    series_key_val: str | None = None
    series_version: str | None = None
    series_position: int | None = None
    cover = parse_cover_letter(parsed.subject)
    in_series = parse_in_series_patch_subject(parsed.subject) if cover is None else None
    # Extract diff-touched paths and review-attestation trailers from
    # the body. The inserts happen later (`_submit_ingest_batch` step 1
    # for ingest, inline for replay) using the returned article id;
    # we just carry the data here. The paths also tell a patch reply
    # from a re-post (#837).
    touched_paths = sorted(extract_touched_paths(parsed.body))
    trailer_rows = extract_trailers(parsed.body)
    # Replay has no session and a parent ingested after its child is not
    # found: either way a reply that repeats a cover's subject is stored
    # as a cover until the next `backfill-patch-series --reprocess` (#820),
    # and one that repeats a patch's subject keeps its position until the
    # next `backfill-patch-series` (#837).
    parent = None
    if (
        session is not None
        and thread_parent is not None
        and (cover is not None or in_series is not None)
    ):
        parent = _thread_parent(session, thread_parent, pending_articles)
    if cover is not None:
        if parent is None or not reply_to_cover(
            parsed.subject, parsed.author, parent.subject, parent.author
        ):
            series_key_val = series_key(cover.title, parsed.author)
            series_version = cover.version
            series_position = 0
    elif in_series is not None and (
        parent is None
        or not reply_to_patch(
            parsed.subject,
            parsed.author,
            parent.subject,
            parent.author,
            bool(touched_paths),
        )
    ):
        series_position = in_series.position
        # The cover, up through any patch this one answers (#840). No head:
        # the batch's re-chain writes it in the same transaction (#816).
        # The first hop reuses `parent`; when that is None (replay, no
        # parent, or one not stored yet) the walk stops before any lookup.
        link = in_series_link(
            parsed.subject,
            thread_parent,
            lambda mid: (
                parent
                if mid == thread_parent
                else _thread_parent(session, mid, pending_articles)
            ),
        )
        if link is not None:
            series_key_val, series_version = link

    return _ArticleInsert(
        message_id=parsed.message_id,
        subject=parsed.subject,
        author=parsed.author,
        date=date,
        thread_parent=thread_parent,
        subject_normalized=normalize_subject(parsed.subject),
        canonical_inbox_id=canonical_inbox_id,
        patch_series_key=series_key_val,
        patch_series_version=series_version,
        patch_series_position=series_position,
        touched_paths=touched_paths,
        trailer_rows=trailer_rows,
    )


def ingest_epoch(
    session: Session,
    inbox: Inbox,
    epoch_name: str,
    repo_path: Path,
    *,
    writer: WriterThread | None = None,
    batch_flush_seconds: float | None = None,
    limit: int | None = None,
    workers: int = DEFAULT_WORKERS,
) -> IngestResult:
    """Per-epoch ingest walk (Phase 3b).

    `session` is a query_only read session (from the caller's
    ReadSessionPool). Per-message decisions accumulate into a
    `_PendingWrites` carrier; at each batch boundary the carrier is
    submitted as one composite WriteOp via `_submit_ingest_batch` and
    the walker awaits `.result()`. After the writer commits,
    `session.commit()` releases the read snapshot so the next batch's
    dedup SELECTs see the committed writes (Design Decision 7 from the
    Phase 3b plan).

    `writer` defaults to `_context.get_active_writer()` when None (the
    normal production path, where `ingest_inbox` sets up the active
    context). Pass explicitly when calling outside a broker context
    (e.g. tests that want a specific WriterThread instance).

    `batch_flush_seconds` defaults to `settings.ingest_batch_flush_seconds`
    when None; keyword-only to avoid silent positional confusion.
    """
    # Import WriterThread type lazily to avoid a circular import at the
    # module level (epoch.py is imported before the broker package in
    # some test paths).
    from mimir.broker.writes import WriterThread  # noqa: F401 (type hint)
    from mimir.ingest._pending import (
        _ArticleListInsert,
        _ParseFailureRecord,
        _PendingWrites,
        _submit_ingest_batch,
    )

    # Resolve the writer from the active context when not passed explicitly.
    if writer is None:
        from mimir.broker._context import get_active_writer

        writer = get_active_writer()  # raises RuntimeError when not set

    flush_seconds = (
        batch_flush_seconds
        if batch_flush_seconds is not None
        else settings.ingest_batch_flush_seconds
    )

    inbox_id = inbox.id
    inbox_name = inbox.name

    # Read the resume cursor (or create an empty state row placeholder
    # in the pending writes rather than in the session - the first
    # flush_batch will upsert the state row).
    last_sha = session.execute(
        select(IngestState.last_commit_sha).where(
            IngestState.inbox_id == inbox_id,
            IngestState.epoch == epoch_name,
        )
    ).scalar_one_or_none()

    result = IngestResult(epoch=epoch_name, last_commit_sha=last_sha)
    last_seen = last_sha
    processed = 0
    seen_in_batch: set[str] = set()

    # SHAs that previously failed to parse for this (inbox, epoch).
    failed_shas: set[str] = set(
        session.execute(
            select(ParseFailure.commit_sha).where(
                ParseFailure.inbox_id == inbox_id,
                ParseFailure.epoch == epoch_name,
            )
        ).scalars()
    )

    # Snapshot of {list_address: inbox_id} for canonical resolution.
    address_to_inbox_id: dict[str, int] = dict(
        session.execute(
            select(Inbox.list_address, Inbox.id).where(Inbox.list_address.isnot(None))
        ).all()
    )

    # Inbox IDs to treat as firehoses during canonical pick.
    demoted_inbox_ids: frozenset[int] = frozenset(
        session.execute(
            select(Inbox.id).where(Inbox.name.in_(settings.canonical_demoted_inboxes))
        ).scalars()
    )

    # Per-batch pending writes accumulator. Reset at each flush boundary.
    pending = _PendingWrites(inbox_id=inbox_id, epoch=epoch_name)

    # Wall-clock anchor for the batch_flush_seconds time cap.
    last_commit_at = time.monotonic()

    logger.info(
        "%s/%s: starting from %s (workers=%d)",
        inbox_name,
        epoch_name,
        last_sha or "<beginning>",
        workers,
    )

    def flush_batch() -> None:
        nonlocal pending, last_commit_at
        pending.last_commit_sha = last_seen
        _submit_ingest_batch(writer, pending).result(timeout=120)
        # Release the read session's implicit READ TRANSACTION so the next
        # batch's dedup SELECTs see the writes just committed on the
        # writer's connection. SQLite readers stay on their frozen snapshot
        # until session.commit() (a no-op for writes on a query_only=1
        # session) closes the read txn; the next SELECT then opens a fresh
        # snapshot. Design Decision 7 from the Phase 3b plan.
        session.commit()
        pending = _PendingWrites(inbox_id=inbox_id, epoch=epoch_name)
        seen_in_batch.clear()
        last_commit_at = time.monotonic()

    walker = _walk_epoch(repo_path, since_sha=last_sha)
    if limit is not None:
        walker = islice(walker, limit)

    for commit_sha, commit_time, parsed_or_exc in _parse_iter(walker, workers=workers):
        last_seen = commit_sha
        processed += 1
        if isinstance(parsed_or_exc, Exception):
            # First-time failures are WARNINGs; re-encounters of an
            # already-recorded failure drop to DEBUG so a reindex pass
            # over a long archive with untriagable blobs doesn't flood
            # the journal.
            already_recorded = commit_sha in failed_shas
            log = logger.debug if already_recorded else logger.warning
            log(
                "epoch %s commit %s: parse failed: %r",
                epoch_name,
                commit_sha[:12],
                parsed_or_exc,
            )
            pending.parse_failures.append(
                _ParseFailureRecord(
                    inbox_id=inbox_id,
                    epoch=epoch_name,
                    commit_sha=commit_sha,
                    delete=False,
                    error_class=type(parsed_or_exc).__name__,
                    error_message=str(parsed_or_exc)[:1000],
                    already_recorded=already_recorded,
                )
            )
            failed_shas.add(commit_sha)
            result.failed += 1
            continue
        parsed = parsed_or_exc

        # Previously failed, now parses cleanly: emit a DELETE record.
        if commit_sha in failed_shas:
            pending.parse_failures.append(
                _ParseFailureRecord(
                    inbox_id=inbox_id,
                    epoch=epoch_name,
                    commit_sha=commit_sha,
                    delete=True,
                )
            )
            failed_shas.discard(commit_sha)

        # Record list-shaped To/Cc addresses for this inbox.
        list_addrs = extract_list_addresses(parsed.headers)
        if list_addrs:
            obs_time = aware_utc(parsed.date or commit_time)
            for addr in list_addrs:
                prev = pending.address_observations.get(addr)
                if prev is None:
                    pending.address_observations[addr] = (1, obs_time)
                else:
                    cnt, ts = prev
                    pending.address_observations[addr] = (cnt + 1, max(ts, obs_time))

        # The one pick function for first copy, cross-post link and the
        # no-op insert of a parallel batch: the writer calls it with the
        # links as they stand at write time (#722).
        repick = partial(
            pick_canonical_inbox_id,
            list_addrs,
            address_to_inbox_id,
            demoted_inbox_ids=demoted_inbox_ids,
        )

        if parsed.message_id in seen_in_batch:
            logger.debug(
                "epoch %s commit %s: skip (in-batch dup) %s",
                epoch_name,
                commit_sha[:12],
                parsed.message_id,
            )
            result.dup_batch += 1
            continue

        # One round-trip for "is the message known, and is it already
        # linked to this inbox?"
        existing_row = session.execute(
            select(
                Article.id,
                Article.date,
                ArticleList.article_id.label("linked_id"),
            )
            .select_from(Article)
            .join(
                ArticleList,
                (ArticleList.article_id == Article.id)
                & (ArticleList.inbox_id == inbox_id),
                isouter=True,
            )
            .where(Article.message_id == parsed.message_id)
        ).one_or_none()
        if existing_row is not None:
            existing_article_id = existing_row.id
            already_linked = existing_row.linked_id
            if already_linked is not None:
                logger.debug(
                    "%s/%s commit %s: skip (already linked) %s",
                    inbox_name,
                    epoch_name,
                    commit_sha[:12],
                    parsed.message_id,
                )
                result.dup_db += 1
            else:
                # Cross-post: link to existing article. The writer
                # re-picks the pin over the links it finds at write time,
                # so a topical copy arriving after the firehose copy
                # upgrades it (#675) even when another batch links first.
                pending.article_lists.append(
                    _ArticleListInsert(
                        article_index=-1,
                        existing_article_id=existing_article_id,
                        inbox_id=inbox_id,
                        epoch=epoch_name,
                        commit_sha=commit_sha,
                        repick=repick,
                    )
                )
                seen_in_batch.add(parsed.message_id)
                result.linked += 1
                if existing_row.date is not None:
                    link_date = aware_utc(existing_row.date)
                    if (
                        pending.last_article_date_candidate is None
                        or link_date > pending.last_article_date_candidate
                    ):
                        pending.last_article_date_candidate = link_date
                logger.debug(
                    "%s/%s commit %s: linked (cross-post) %s",
                    inbox_name,
                    epoch_name,
                    commit_sha[:12],
                    parsed.message_id,
                )
            continue

        canonical_inbox_id = repick(frozenset({inbox_id}))
        art_insert = _to_article_insert(
            parsed,
            inbox_id=inbox_id,
            epoch=epoch_name,
            commit_sha=commit_sha,
            date=commit_time,
            canonical_inbox_id=canonical_inbox_id,
            session=session,
            pending_articles=pending.articles,
        )
        article_index = len(pending.articles)
        art_insert.repick = repick
        pending.articles.append(art_insert)
        pending.article_lists.append(
            _ArticleListInsert(
                article_index=article_index,
                existing_article_id=None,
                inbox_id=inbox_id,
                epoch=epoch_name,
                commit_sha=commit_sha,
            )
        )
        seen_in_batch.add(parsed.message_id)
        result.new += 1
        result.new_message_ids.append(parsed.message_id)
        if (
            pending.last_article_date_candidate is None
            or commit_time > pending.last_article_date_candidate
        ):
            pending.last_article_date_candidate = commit_time
        logger.debug(
            "%s/%s commit %s: new %s",
            inbox_name,
            epoch_name,
            commit_sha[:12],
            parsed.message_id,
        )

        if processed % PROGRESS_EVERY == 0:
            logger.info(
                "%s/%s: processed=%d new=%d linked=%d dup_batch=%d dup_db=%d failed=%d",
                inbox_name,
                epoch_name,
                processed,
                result.new,
                result.linked,
                result.dup_batch,
                result.dup_db,
                result.failed,
            )

        # Flush at the message-count boundary (large bursts) OR when
        # the wall-clock cap fires (keeps writer-lock-hold bounded so
        # concurrent cache.set RPCs drain between batches).
        if (
            processed % COMMIT_EVERY == 0
            or (time.monotonic() - last_commit_at) >= flush_seconds
        ):
            flush_batch()

    flush_batch()

    result.last_commit_sha = last_seen
    return result
