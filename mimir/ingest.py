import logging
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Iterable, Iterator

from dulwich.errors import NotGitRepository
from dulwich.repo import Repo
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from mimir.canonical import extract_list_addresses, pick_canonical_inbox_id
from mimir.config import settings
from mimir.extensions import SessionLocal, engine
from mimir.models import (
    Article,
    ArticleList,
    Inbox,
    InboxAddressObservation,
    IngestState,
    ParseFailure,
)
from mimir.parser import ParsedArticle, normalize_subject, parse_message
from mimir.store import MessageNotFound, read_message

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 100
COMMIT_EVERY = 500
DEFAULT_WORKERS = os.cpu_count() or 1
PARSE_CHUNKSIZE = 50

def _aware_utc(dt: datetime) -> datetime:
    """Return `dt` as a tz-aware UTC datetime. RFC 5322 dates with
    `-0000` come back from `email.utils.parsedate_to_datetime` as
    naive — mixing those into a max() with tz-aware dates raises
    TypeError, which would crash an entire ingest batch the moment
    one such message landed. Cheap normalisation at every entry
    keeps the tally rows uniformly comparable."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# Auto-promotion of `Inbox.list_address`: an inbox needs at least
# MIN_PROMOTE_OBSERVATIONS messages observed before the modal address
# can be trusted, AND the modal address must account for at least
# PROMOTE_DOMINANCE of the top-two combined to count as a clear winner.
# Tuned conservative — false promotion would silently misroute canonical
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
    # Article already in DB and already linked to this inbox — usually
    # a re-walk (rewound IngestState, reindex without --from-scratch),
    # or a public-inbox archive that committed the same `m` blob twice.
    dup_db: int = 0
    failed: int = 0
    last_commit_sha: str | None = None


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
    or the exception object — exceptions can't propagate out of pool.map cleanly
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
) -> Article:
    """Construct a brand-new Article (with one ArticleList row) for a
    message we haven't seen before. For cross-posts (already-known
    Message-ID), the caller adds an ArticleList row directly instead."""
    thread_parent = parsed.in_reply_to or (
        parsed.references[-1] if parsed.references else None
    )
    return Article(
        message_id=parsed.message_id,
        subject=parsed.subject,
        author=parsed.author,
        date=date,
        thread_parent=thread_parent,
        subject_normalized=normalize_subject(parsed.subject),
        canonical_inbox_id=canonical_inbox_id,
        lists=[ArticleList(inbox_id=inbox_id, epoch=epoch, commit_sha=commit_sha)],
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
            # SQLite's scalar max(a, b) — keeps the freshest timestamp
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
    # practice — typical clean parsers fail on <0.1% of messages.
    failed_shas: set[str] = set(session.execute(
        select(ParseFailure.commit_sha).where(
            ParseFailure.inbox_id == inbox_id,
            ParseFailure.epoch == epoch_name,
        )
    ).scalars())

    # Snapshot of {list_address: inbox_id} for canonical resolution.
    # Refreshed at start; promotion that happens later in this run
    # affects future runs, not in-flight messages — acceptable lag for
    # bootstrap, and Phase 2's backfill CLI sweeps the gap.
    address_to_inbox_id: dict[str, int] = dict(session.execute(
        select(Inbox.list_address, Inbox.id).where(Inbox.list_address.isnot(None))
    ).all())

    # Per-address observation deltas accumulated this batch; flushed to
    # `inbox_address_observations` on each commit so we don't issue a
    # write per message.
    pending_obs: dict[str, tuple[int, datetime]] = {}

    logger.info(
        "%s/%s: starting from %s (workers=%d)",
        inbox_name, epoch_name, last_sha or "<beginning>", workers,
    )

    def flush_batch() -> None:
        if pending_obs:
            _flush_observations(session, inbox_id, pending_obs)
            pending_obs.clear()
        state.last_commit_sha = last_seen
        session.commit()
        seen_in_batch.clear()

    walker = _walk_epoch(repo_path, since_sha=last_sha)
    if limit is not None:
        walker = islice(walker, limit)

    for commit_sha, commit_time, parsed_or_exc in _parse_iter(walker, workers=workers):
        last_seen = commit_sha
        processed += 1
        if isinstance(parsed_or_exc, Exception):
            logger.warning(
                "epoch %s commit %s: parse failed: %r",
                epoch_name, commit_sha[:12], parsed_or_exc,
            )
            _record_parse_failure(
                session, inbox_id, epoch_name, commit_sha, parsed_or_exc,
                already_recorded=commit_sha in failed_shas,
            )
            failed_shas.add(commit_sha)
            result.failed += 1
            continue
        parsed = parsed_or_exc

        # Previously failed, now parses cleanly — clear the row.
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
        # addresses, which is what we want — the modal address per
        # inbox surfaces correctly).
        list_addrs = extract_list_addresses(parsed.headers)
        if list_addrs:
            obs_time = _aware_utc(parsed.date or commit_time)
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

        existing_article_id = session.execute(
            select(Article.id).where(Article.message_id == parsed.message_id)
        ).scalar_one_or_none()
        if existing_article_id is not None:
            # Already-known message. Either we re-ingested this inbox
            # (skip) or it's a cross-post first seen via another inbox
            # (add the link).
            already_linked = session.execute(
                select(ArticleList.article_id).where(
                    ArticleList.article_id == existing_article_id,
                    ArticleList.inbox_id == inbox_id,
                )
            ).scalar_one_or_none()
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
                logger.debug("%s/%s commit %s: linked (cross-post) %s",
                             inbox_name, epoch_name, commit_sha[:12], parsed.message_id)
            continue

        canonical_inbox_id = pick_canonical_inbox_id(list_addrs, address_to_inbox_id)
        session.add(_to_article(
            parsed, inbox_id=inbox_id, epoch=epoch_name,
            commit_sha=commit_sha, date=commit_time,
            canonical_inbox_id=canonical_inbox_id,
        ))
        seen_in_batch.add(parsed.message_id)
        result.new += 1
        logger.debug("%s/%s commit %s: new %s", inbox_name, epoch_name, commit_sha[:12], parsed.message_id)

        if processed % PROGRESS_EVERY == 0:
            logger.info(
                "%s/%s: processed=%d new=%d linked=%d dup_batch=%d dup_db=%d failed=%d",
                inbox_name, epoch_name, processed,
                result.new, result.linked, result.dup_batch, result.dup_db, result.failed,
            )

        if processed % COMMIT_EVERY == 0:
            flush_batch()

    flush_batch()

    result.last_commit_sha = last_seen
    return result


class ReplayResult(BaseModel):
    """Outcome of replaying persisted parse failures for one inbox."""
    attempted: int = 0
    recovered: int = 0   # parsed cleanly; row deleted, article inserted/linked.
    still_failed: int = 0  # parse still raises; row's last_attempt + attempts updated.
    skipped: int = 0     # blob couldn't be fetched (mirror missing, ref pruned).


def replay_failures(
    inbox: Inbox,
    epoch_filter: str | None = None,
    limit: int | None = None,
) -> ReplayResult:
    """Re-parse persisted parse_failures rows for `inbox`.

    On success: insert the Article (or cross-post link) and delete the
    failure row. On failure: bump attempts/last_attempt and refresh the
    error fields. Sequential by design — replay is a low-volume admin
    op, not the hot ingest path.
    """
    out = ReplayResult()
    with SessionLocal() as session:
        attached = session.merge(inbox)
        q = select(ParseFailure).where(ParseFailure.inbox_id == attached.id)
        if epoch_filter is not None:
            q = q.where(ParseFailure.epoch == epoch_filter)
        q = q.order_by(ParseFailure.epoch, ParseFailure.commit_sha)
        if limit is not None:
            q = q.limit(limit)
        rows = list(session.execute(q).scalars())

        # Group by epoch so we open each dulwich repo once.
        repo_cache: dict[str, Repo] = {}
        for row in rows:
            out.attempted += 1
            repo_path = Path(attached.mirror_path) / row.epoch
            repo = repo_cache.get(row.epoch)
            if repo is None:
                try:
                    repo = Repo(str(repo_path))
                except (NotGitRepository, FileNotFoundError):
                    out.skipped += 1
                    continue
                repo_cache[row.epoch] = repo

            try:
                commit = repo[row.commit_sha.encode()]
                tree = repo[commit.tree]
                _mode, blob_sha = tree[b"m"]
                raw = repo[blob_sha].data
                commit_time = datetime.fromtimestamp(commit.commit_time, timezone.utc)
            except KeyError:
                # Commit or `m` blob missing — mirror was pruned or
                # rewound. Leave the row in place; surface to operator.
                out.skipped += 1
                continue

            try:
                parsed = parse_message(raw)
            except Exception as exc:
                row.last_attempt = datetime.now(timezone.utc)
                row.attempts += 1
                row.error_class = type(exc).__name__
                row.error_message = str(exc)[:1000]
                out.still_failed += 1
                continue

            existing_id = session.execute(
                select(Article.id).where(Article.message_id == parsed.message_id)
            ).scalar_one_or_none()
            if existing_id is None:
                session.add(_to_article(
                    parsed, inbox_id=attached.id, epoch=row.epoch,
                    commit_sha=row.commit_sha, date=commit_time,
                ))
            else:
                # Cross-post: link if not already linked. We only ever
                # have a failure row for a SHA whose article wasn't
                # successfully ingested in *this* inbox, but be defensive
                # against the (rare) case where another path inserted it.
                already_linked = session.execute(
                    select(ArticleList.article_id).where(
                        ArticleList.article_id == existing_id,
                        ArticleList.inbox_id == attached.id,
                    )
                ).scalar_one_or_none()
                if already_linked is None:
                    session.add(ArticleList(
                        article_id=existing_id,
                        inbox_id=attached.id,
                        epoch=row.epoch,
                        commit_sha=row.commit_sha,
                    ))
            session.delete(row)
            out.recovered += 1
        session.commit()
    return out


class BackfillResult(BaseModel):
    """Outcome counters for `backfill_canonicals`. Each examined article
    lands in exactly one of resolved/unresolved/skipped (resolved =
    `canonical_inbox_id` was set or updated; unresolved = parsed cleanly
    but no list address matched; skipped = blob couldn't be read)."""
    examined: int = 0
    resolved: int = 0
    unresolved: int = 0
    skipped: int = 0


def backfill_canonicals(
    inbox_filter: str | None = None,
    limit: int | None = None,
    reprocess: bool = False,
    promote_every: int = 200,
    progress_every: int = 1000,
    progress: callable = None,  # type: ignore[valid-type]
) -> BackfillResult:
    """Walk historical articles newest-first, record per-inbox address
    observations and resolve `canonical_inbox_id` from the original
    To/Cc headers. Idempotent + resumable: by default skips articles
    that already have a canonical set (the `WHERE canonical_inbox_id IS
    NULL` filter naturally picks up where a previous run left off).

    Mid-walk, every `promote_every` articles we re-run
    `_maybe_promote_list_address` for inboxes still at NULL — that way
    auto-promotion fires early in the pass (after the first ~50 newest
    messages per inbox accumulate observations) and the bulk of the
    walk resolves canonicals against a settled list_address map.

    `--reprocess` re-examines articles whose canonical is already set
    (use after operator updates a list_address or when chasing the
    bootstrap region from an earlier pass). `--inbox` restricts the
    walk to articles linked to a single inbox. `--limit` caps the
    session for "do an hour's worth tonight."
    """
    out = BackfillResult()
    address_to_inbox_id: dict[str, int] = {}
    pending_obs: dict[int, dict[str, tuple[int, datetime]]] = {}
    inbox_cache: dict[int, Inbox] = {}

    def refresh_address_map(session: Session) -> None:
        nonlocal address_to_inbox_id
        address_to_inbox_id = dict(session.execute(
            select(Inbox.list_address, Inbox.id).where(Inbox.list_address.isnot(None))
        ).all())

    def get_inbox(session: Session, inbox_id: int) -> Inbox:
        ix = inbox_cache.get(inbox_id)
        if ix is None:
            ix = session.get(Inbox, inbox_id)
            assert ix is not None, f"missing inbox row for id={inbox_id}"
            inbox_cache[inbox_id] = ix
        return ix

    def flush_pending(session: Session) -> None:
        for inbox_id, obs in pending_obs.items():
            if obs:
                _flush_observations(session, inbox_id, obs)
        pending_obs.clear()

    def maybe_promote_all(session: Session) -> bool:
        """Run promotion for every inbox that's still NULL. Returns True
        if any inbox got promoted (caller refreshes the address map)."""
        promoted = False
        for ix in session.execute(
            select(Inbox).where(Inbox.list_address.is_(None))
        ).scalars():
            if _maybe_promote_list_address(session, ix.id) is not None:
                promoted = True
        return promoted

    with SessionLocal() as session:
        refresh_address_map(session)

        # Build the article query. Newest-first so the most-indexed
        # articles get correct canonicals first AND auto-promotion has
        # the freshest observations to work with.
        q = select(Article).order_by(Article.date.desc().nullslast())
        if not reprocess:
            q = q.where(Article.canonical_inbox_id.is_(None))
        if inbox_filter is not None:
            q = q.join(ArticleList, ArticleList.article_id == Article.id) \
                 .join(Inbox, Inbox.id == ArticleList.inbox_id) \
                 .where(Inbox.name == inbox_filter)
        if limit is not None:
            q = q.limit(limit)

        articles = list(session.execute(q).scalars())
        for article in articles:
            out.examined += 1

            # Pick a linked inbox to read the blob from. Cross-posts
            # have one ArticleList row per inbox, all pointing at the
            # same logical message; first try the lowest-id inbox.
            links = list(session.execute(
                select(ArticleList.inbox_id)
                .where(ArticleList.article_id == article.id)
                .order_by(ArticleList.inbox_id)
            ).scalars())

            parsed = None
            for inbox_id in links:
                ix = get_inbox(session, inbox_id)
                try:
                    parsed = read_message(session, ix, article.message_id)
                    break
                except MessageNotFound as exc:
                    logger.debug(
                        "backfill: %s blob not in %s: %s",
                        article.message_id, ix.name, exc,
                    )
                    continue
                except Exception as exc:
                    logger.warning(
                        "backfill: parse failed for %s in %s: %r",
                        article.message_id, ix.name, exc,
                    )
                    continue
            if parsed is None:
                out.skipped += 1
                continue

            list_addrs = extract_list_addresses(parsed.headers)
            if list_addrs:
                obs_time = _aware_utc(
                    parsed.date or article.date or datetime.now(timezone.utc)
                )
                for inbox_id in links:
                    bucket = pending_obs.setdefault(inbox_id, {})
                    for addr in list_addrs:
                        prev = bucket.get(addr)
                        if prev is None:
                            bucket[addr] = (1, obs_time)
                        else:
                            cnt, ts = prev
                            bucket[addr] = (cnt + 1, max(ts, obs_time))

            new_canonical = pick_canonical_inbox_id(list_addrs, address_to_inbox_id)
            if new_canonical != article.canonical_inbox_id:
                article.canonical_inbox_id = new_canonical
                out.resolved += 1
            else:
                out.unresolved += 1

            if out.examined % promote_every == 0:
                flush_pending(session)
                session.commit()
                if maybe_promote_all(session):
                    refresh_address_map(session)
                    session.commit()

            if progress is not None and out.examined % progress_every == 0:
                progress(out)

        flush_pending(session)
        maybe_promote_all(session)
        session.commit()

    return out


def discover_epochs(mirror_path: Path) -> list[Path]:
    epochs = []
    for child in sorted(mirror_path.iterdir()):
        if not child.is_dir():
            continue
        try:
            Repo(str(child))
        except NotGitRepository:
            continue
        epochs.append(child)
    return epochs


def ingest_inbox(
    inbox: Inbox,
    limit: int | None = None,
    workers: int = DEFAULT_WORKERS,
) -> list[IngestResult]:
    """Ingest every epoch under one inbox's mirror path."""
    results: list[IngestResult] = []
    remaining = limit
    with SessionLocal() as session:
        # Re-attach the Inbox to this session so .id reads work after
        # the caller's session was closed.
        attached = session.merge(inbox)
        for epoch_path in discover_epochs(Path(attached.mirror_path)):
            if remaining is not None and remaining <= 0:
                break
            r = ingest_epoch(
                session, attached, epoch_path.name, epoch_path,
                limit=remaining, workers=workers,
            )
            results.append(r)
            if remaining is not None:
                remaining -= r.new + r.linked + r.dup_batch + r.dup_db + r.failed

    # Promote `Inbox.list_address` if we now have enough observations.
    # Cheap: at most two rows queried, one update if it fires.
    with SessionLocal() as session:
        _maybe_promote_list_address(session, inbox.id)
        session.commit()

    # Refresh planner stats when we've moved enough rows that prior
    # `sqlite_stat1` can no longer be trusted — most importantly the
    # first ingest of a freshly-added inbox, which lands a whole archive
    # in one go and would otherwise leave the planner blind until the
    # next scheduled ANALYZE.
    threshold = settings.analyze_after_ingest_rows
    if threshold > 0:
        moved = sum(r.new + r.linked for r in results)
        if moved >= threshold:
            logger.info("auto-ANALYZE after %s/%d rows ingested", inbox.name, moved)
            with engine.begin() as conn:
                conn.execute(text("ANALYZE"))

    return results


def ingest_all(
    inboxes: dict[str, Inbox] | None = None,
    limit: int | None = None,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, list[IngestResult]]:
    """Ingest every supplied inbox. Returns {inbox_name: [IngestResult, ...]}."""
    if inboxes is None:
        from mimir.inboxes import bootstrap_inboxes
        inboxes = bootstrap_inboxes()

    out: dict[str, list[IngestResult]] = {}
    remaining = limit
    for name, inbox in inboxes.items():
        if remaining is not None and remaining <= 0:
            break
        rs = ingest_inbox(inbox, limit=remaining, workers=workers)
        out[name] = rs
        if remaining is not None:
            for r in rs:
                remaining -= r.new + r.linked + r.dup_batch + r.dup_db + r.failed
    return out
