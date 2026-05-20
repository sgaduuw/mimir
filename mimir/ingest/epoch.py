"""Core per-epoch ingest walk + the small helpers it shares with the
replay and backfill flows.

`ingest_epoch` is the hot path: walk a public-inbox epoch's git log
from the last-seen commit forward, parse messages (sequentially or via
a process pool), bucket each outcome (new / linked / dup_batch /
dup_db / failed), and persist a per-epoch resume cursor.

The shared helpers (`_to_article`, `_flush_observations`,
`_maybe_promote_list_address`) and the `IngestResult` model are also
imported by `.replay`, `.backfill`, and `.orchestrate`.
"""
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Iterable, Iterator

from dulwich.repo import Repo
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from mimir.canonical import extract_list_addresses, pick_canonical_inbox_id
from mimir.config import settings
from mimir.datetime_utils import aware_utc
from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    ArticleTrailer,
    Inbox,
    InboxAddressObservation,
    IngestState,
    ParseFailure,
)
from mimir.parser import ParsedArticle, normalize_subject, parse_message
from mimir.patch_revisions import parse_in_series_patch_subject
from mimir.patch_series import parse_cover_letter, series_key
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
# Floor: even hot inboxes (`stable`, `linux-mm`) committing on the
# message-count side already hit COMMIT_EVERY=500 well within this
# wall budget, so the time cap only fires on the steady-state
# few-hundred-messages-per-tick path that was holding the lock for
# 1.7-2.2 s on production 1.36.0. Direct (non-broker) ingests are
# unaffected: same number of commits, just more of them on hot
# inboxes; commit overhead on SQLite WAL is single-digit ms.
COMMIT_EVERY_SECONDS = 0.5
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
    """Yield (commit_sha, commit_time_utc, raw_message_bytes) per epoch commit."""
    repo = Repo(str(repo_path))
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
        commit_time = datetime.fromtimestamp(commit.commit_time, timezone.utc)
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


def _record_parse_failure(
    session: Session,
    inbox_id: int,
    epoch: str,
    commit_sha: str,
    exc: Exception,
    *,
    already_recorded: bool,
) -> None:
    """Persist (or refresh) a parse_failures row for one bad commit.
    `already_recorded=True` means the row exists and we just bump
    last_attempt + attempts and refresh the error fields (the cause may
    have shifted between parser versions). Otherwise insert fresh."""
    now = datetime.now(timezone.utc)
    error_class = type(exc).__name__
    error_message = str(exc)[:1000]  # cap pathological repr/str
    if already_recorded:
        row = session.get(ParseFailure, (inbox_id, epoch, commit_sha))
        if row is not None:
            row.last_attempt = now
            row.attempts += 1
            row.error_class = error_class
            row.error_message = error_message
            return
    session.add(ParseFailure(
        inbox_id=inbox_id,
        epoch=epoch,
        commit_sha=commit_sha,
        error_class=error_class,
        error_message=error_message,
        first_seen=now,
        last_attempt=now,
        attempts=1,
    ))


def _to_article(
    parsed: ParsedArticle,
    inbox_id: int,
    epoch: str,
    commit_sha: str,
    date: datetime,
    canonical_inbox_id: int | None = None,
    session: Session | None = None,
) -> Article:
    """Construct a brand-new Article (with one ArticleList row) for a
    message we haven't seen before. For cross-posts (already-known
    Message-ID), the caller adds an ArticleList row directly instead.

    `session` is required to populate `patch_series_key` / `version`
    on in-series patches via a thread-parent lookup; without it,
    in-series patches land with position set but key + version NULL
    and need the backfill command to fill the gap.
    """
    thread_parent = parsed.in_reply_to or (
        parsed.references[-1] if parsed.references else None
    )
    # Extract diff-touched paths for patch bodies. Empty set on non-
    # patch articles. ArticleFile rows take their article_id at flush
    # via the back-populated relationship, no manual id wiring.
    touched_paths = extract_touched_paths(parsed.body)
    # Same shape for review-attestation trailers: pure parse over
    # parsed.body, ArticleTrailer rows wired through the back-populated
    # `trailers` relationship.
    trailer_rows = [
        ArticleTrailer(
            role=role,
            name=name,
            address=address,
            address_normalized=address.lower(),
        )
        for role, name, address in extract_trailers(parsed.body)
    ]
    # Patch-series identity, two write paths (#212):
    # - Cover letter (`[PATCH ... 0/N]`): key + version derived from
    #   the subject + author; position = 0.
    # - In-series patch (`[PATCH ... M/T]` with M >= 1): position = M
    #   from the parser; key + version copied from the thread parent
    #   when that parent is a cover letter already in the DB.
    # Otherwise (prose, replies, solo `[PATCH]`, `[GIT PULL]` etc.)
    # all three columns stay NULL.
    series_key_val: str | None = None
    series_version: str | None = None
    series_position: int | None = None
    cover = parse_cover_letter(parsed.subject)
    if cover is not None:
        series_key_val = series_key(cover.title, parsed.author)
        series_version = cover.version
        series_position = 0
    else:
        in_series = parse_in_series_patch_subject(parsed.subject)
        if in_series is not None:
            series_position = in_series.position
            if session is not None and thread_parent is not None:
                # Direct-reply linkage is the modal `git send-email
                # --thread` shape. Two-step lookup: same-batch
                # pending Articles via `session.new` (we don't
                # autoflush; an already-added cover in this batch
                # isn't visible to SELECT yet), then SQL for already-
                # committed parents. Skip when the parent has no key
                # (orphan parent, or this thread isn't series-shaped
                # after all); backfill closes the gap later.
                pending_key = pending_version = None
                for obj in session.new:
                    if (
                        isinstance(obj, Article)
                        and obj.message_id == thread_parent
                        and obj.patch_series_key is not None
                    ):
                        pending_key = obj.patch_series_key
                        pending_version = obj.patch_series_version
                        break
                if pending_key is not None:
                    series_key_val = pending_key
                    series_version = pending_version
                else:
                    row = session.execute(
                        select(
                            Article.patch_series_key,
                            Article.patch_series_version,
                        ).where(
                            Article.message_id == thread_parent,
                            Article.patch_series_key.isnot(None),
                        )
                    ).one_or_none()
                    if row is not None:
                        series_key_val = row.patch_series_key
                        series_version = row.patch_series_version
    return Article(
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
        lists=[ArticleList(inbox_id=inbox_id, epoch=epoch, commit_sha=commit_sha)],
        files=[ArticleFile(path=p) for p in sorted(touched_paths)],
        trailers=trailer_rows,
    )


def _flush_observations(
    session: Session,
    inbox_id: int,
    pending: dict[str, tuple[int, datetime]],
) -> None:
    """Upsert pending in-memory observations for one inbox. Counts are
    additive (existing + delta); last_seen is the max of existing and
    incoming so a stale tail doesn't roll back a fresher timestamp."""
    if not pending:
        return
    rows = [
        {"inbox_id": inbox_id, "address": addr, "count": count, "last_seen": last_seen}
        for addr, (count, last_seen) in pending.items()
    ]
    stmt = sqlite_insert(InboxAddressObservation).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["inbox_id", "address"],
        set_={
            "count": InboxAddressObservation.count + stmt.excluded.count,
            # SQLite's scalar max(a, b), keeps the freshest timestamp
            # when a stale tail-end batch lands after a newer one.
            "last_seen": func.max(
                InboxAddressObservation.last_seen, stmt.excluded.last_seen
            ),
        },
    )
    session.execute(stmt)


def _maybe_promote_list_address(session: Session, inbox_id: int) -> str | None:
    """If this inbox has no `list_address` yet AND the observed-address
    tally has a clear modal winner, promote it. Returns the promoted
    address, or None if nothing happened (already set, not enough
    samples, or no clear winner). Idempotent: caller can run on every
    ingest tick; only the first qualifying call writes."""
    inbox = session.get(Inbox, inbox_id)
    if inbox is None or inbox.list_address is not None:
        return None
    rows = session.execute(
        select(
            InboxAddressObservation.address,
            InboxAddressObservation.count,
        )
        .where(InboxAddressObservation.inbox_id == inbox_id)
        .order_by(InboxAddressObservation.count.desc())
        .limit(2)
    ).all()
    if not rows:
        return None
    top_addr, top_count = rows[0]
    if top_count < MIN_PROMOTE_OBSERVATIONS:
        return None
    second_count = rows[1][1] if len(rows) > 1 else 0
    if top_count / max(top_count + second_count, 1) < PROMOTE_DOMINANCE:
        return None
    inbox.list_address = top_addr
    logger.info(
        "auto-promoted list_address: %s -> %s (n=%d, dominance=%.0f%%)",
        inbox.name, top_addr, top_count,
        100 * top_count / max(top_count + second_count, 1),
    )
    return top_addr


def ingest_epoch(
    session: Session,
    inbox: Inbox,
    epoch_name: str,
    repo_path: Path,
    limit: int | None = None,
    workers: int = DEFAULT_WORKERS,
) -> IngestResult:
    inbox_id = inbox.id
    inbox_name = inbox.name

    state = session.get(IngestState, (inbox_id, epoch_name))
    if state is None:
        state = IngestState(inbox_id=inbox_id, epoch=epoch_name)
        session.add(state)
    last_sha = state.last_commit_sha

    result = IngestResult(epoch=epoch_name, last_commit_sha=last_sha)
    last_seen = last_sha
    processed = 0
    seen_in_batch: set[str] = set()

    # SHAs that previously failed to parse for this (inbox, epoch).
    # Used to (a) skip the cleanup DELETE on the hot fresh-ingest path
    # (set is empty for never-failed epochs) and (b) clear the row when
    # a previously-failed commit now parses cleanly. Tiny set in
    # practice, typical clean parsers fail on <0.1% of messages.
    failed_shas: set[str] = set(session.execute(
        select(ParseFailure.commit_sha).where(
            ParseFailure.inbox_id == inbox_id,
            ParseFailure.epoch == epoch_name,
        )
    ).scalars())

    # Snapshot of {list_address: inbox_id} for canonical resolution.
    # Refreshed at start; promotion that happens later in this run
    # affects future runs, not in-flight messages, acceptable lag for
    # bootstrap, and Phase 2's backfill CLI sweeps the gap.
    address_to_inbox_id: dict[str, int] = dict(session.execute(
        select(Inbox.list_address, Inbox.id).where(Inbox.list_address.isnot(None))
    ).all())

    # Inbox IDs to treat as firehoses during canonical pick (see
    # `pick_canonical_inbox_id`). Computed once per run; name list
    # comes from `Settings.canonical_demoted_inboxes`.
    demoted_inbox_ids: frozenset[int] = frozenset(session.execute(
        select(Inbox.id).where(Inbox.name.in_(settings.canonical_demoted_inboxes))
    ).scalars())

    # Per-address observation deltas accumulated this batch; flushed to
    # `inbox_address_observations` on each commit so we don't issue a
    # write per message.
    pending_obs: dict[str, tuple[int, datetime]] = {}

    # Max article date seen this batch (across `new` AND `linked`
    # outcomes, so cross-posts first seen elsewhere still bump the
    # destination inbox's "Last activity"). Flushed to
    # `Inbox.last_article_date` on each commit so the front-page card
    # doesn't ride the 24h `archive_stats` cache. See #216.
    pending_max_date: datetime | None = None

    # Wall-clock anchor for the `COMMIT_EVERY_SECONDS` time cap. Reset
    # to the current monotonic time inside `flush_batch` so every
    # commit window starts fresh; an exception that propagates out
    # before `flush_batch` runs would leave a stale anchor, but the
    # surrounding `write_transaction` rolls back the in-flight batch
    # in that case so the stale value is moot.
    last_commit_at = time.monotonic()

    logger.info(
        "%s/%s: starting from %s (workers=%d)",
        inbox_name, epoch_name, last_sha or "<beginning>", workers,
    )

    def flush_batch() -> None:
        nonlocal pending_max_date, last_commit_at
        if pending_obs:
            _flush_observations(session, inbox_id, pending_obs)
            pending_obs.clear()
        if pending_max_date is not None:
            # `inbox` may be detached (callers sometimes pass an Inbox
            # fetched in another session); re-fetch by id so the mutation
            # lands on a session-attached row that SQLAlchemy will flush.
            ib = session.get(Inbox, inbox_id)
            if ib is not None:
                current = ib.last_article_date
                if current is None or aware_utc(current) < pending_max_date:
                    ib.last_article_date = pending_max_date
            pending_max_date = None
        state.last_commit_sha = last_seen
        session.commit()
        seen_in_batch.clear()
        last_commit_at = time.monotonic()

    walker = _walk_epoch(repo_path, since_sha=last_sha)
    if limit is not None:
        walker = islice(walker, limit)

    for commit_sha, commit_time, parsed_or_exc in _parse_iter(walker, workers=workers):
        last_seen = commit_sha
        processed += 1
        if isinstance(parsed_or_exc, Exception):
            # First-time failures are WARNINGs (real new event the
            # operator should know about); re-encounters of an already-
            # recorded failure drop to DEBUG so a reindex pass over a
            # long archive with a stable set of untriagable blobs (RFC
            # 5322 violators, oversized payloads) doesn't drown the
            # journal. The `parse_failures` row still tracks everything,
            # this only governs the log line.
            already_recorded = commit_sha in failed_shas
            log = logger.debug if already_recorded else logger.warning
            log(
                "epoch %s commit %s: parse failed: %r",
                epoch_name, commit_sha[:12], parsed_or_exc,
            )
            _record_parse_failure(
                session, inbox_id, epoch_name, commit_sha, parsed_or_exc,
                already_recorded=already_recorded,
            )
            failed_shas.add(commit_sha)
            result.failed += 1
            continue
        parsed = parsed_or_exc

        # Previously failed, now parses cleanly, clear the row.
        if commit_sha in failed_shas:
            session.execute(delete(ParseFailure).where(
                ParseFailure.inbox_id == inbox_id,
                ParseFailure.epoch == epoch_name,
                ParseFailure.commit_sha == commit_sha,
            ))
            failed_shas.discard(commit_sha)

        # Record list-shaped To/Cc addresses for this inbox. Done once
        # per parse so cross-posts contribute to *this* inbox's tally
        # (each linked inbox sees the same message and counts the same
        # addresses, which is what we want, the modal address per
        # inbox surfaces correctly).
        list_addrs = extract_list_addresses(parsed.headers)
        if list_addrs:
            obs_time = aware_utc(parsed.date or commit_time)
            for addr in list_addrs:
                prev = pending_obs.get(addr)
                if prev is None:
                    pending_obs[addr] = (1, obs_time)
                else:
                    cnt, ts = prev
                    pending_obs[addr] = (cnt + 1, max(ts, obs_time))

        if parsed.message_id in seen_in_batch:
            logger.debug("epoch %s commit %s: skip (in-batch dup) %s", epoch_name, commit_sha[:12], parsed.message_id)
            result.dup_batch += 1
            continue

        # One round-trip for "is the message known, and is it already
        # linked to this inbox?" The LEFT JOIN means: zero rows → new
        # article; one row with NULL `linked_id` → existing article
        # not yet linked here (cross-post first seen elsewhere); one
        # row with non-NULL `linked_id` → already linked (dup_db).
        # Halves the query count on the 6M-row backfill cold path
        # (was 1000/batch, now ~500). `Article.date` rides along to
        # bump `Inbox.last_article_date` for cross-posts; the column
        # is on the same row so no extra cost.
        existing_row = session.execute(
            select(
                Article.id, Article.date,
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
            # Already-known message. Either we re-ingested this inbox
            # (skip) or it's a cross-post first seen via another inbox
            # (add the link).
            if already_linked is not None:
                logger.debug("%s/%s commit %s: skip (already linked) %s",
                             inbox_name, epoch_name, commit_sha[:12], parsed.message_id)
                result.dup_db += 1
            else:
                session.add(ArticleList(
                    article_id=existing_article_id,
                    inbox_id=inbox_id,
                    epoch=epoch_name,
                    commit_sha=commit_sha,
                ))
                seen_in_batch.add(parsed.message_id)
                result.linked += 1
                # Cross-post: bump activity using the existing article's
                # date (the link is new to *this* inbox but the article
                # already carries a real date from its first ingest).
                if existing_row.date is not None:
                    link_date = aware_utc(existing_row.date)
                    if pending_max_date is None or link_date > pending_max_date:
                        pending_max_date = link_date
                logger.debug("%s/%s commit %s: linked (cross-post) %s",
                             inbox_name, epoch_name, commit_sha[:12], parsed.message_id)
            continue

        canonical_inbox_id = pick_canonical_inbox_id(
            list_addrs, address_to_inbox_id, demoted_inbox_ids,
        )
        session.add(_to_article(
            parsed, inbox_id=inbox_id, epoch=epoch_name,
            commit_sha=commit_sha, date=commit_time,
            canonical_inbox_id=canonical_inbox_id,
            session=session,
        ))
        seen_in_batch.add(parsed.message_id)
        result.new += 1
        result.new_message_ids.append(parsed.message_id)
        # `commit_time` is already aware-UTC from `_walk_epoch`; same
        # value `_to_article` just persisted to `Article.date`.
        if pending_max_date is None or commit_time > pending_max_date:
            pending_max_date = commit_time
        logger.debug("%s/%s commit %s: new %s", inbox_name, epoch_name, commit_sha[:12], parsed.message_id)

        if processed % PROGRESS_EVERY == 0:
            logger.info(
                "%s/%s: processed=%d new=%d linked=%d dup_batch=%d dup_db=%d failed=%d",
                inbox_name, epoch_name, processed,
                result.new, result.linked, result.dup_batch, result.dup_db, result.failed,
            )

        # Commit at the message-count boundary (large bursts) OR
        # when the wall-clock cap fires (steady-state hot inboxes
        # under broker mode, where the cache worker needs the writer
        # lock back regularly to serve cache.set RPCs). See the
        # COMMIT_EVERY_SECONDS comment above for the why.
        if (
            processed % COMMIT_EVERY == 0
            or (time.monotonic() - last_commit_at) >= COMMIT_EVERY_SECONDS
        ):
            flush_batch()

    flush_batch()

    result.last_commit_sha = last_seen
    return result
