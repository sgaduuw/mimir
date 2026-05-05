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
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, Inbox, IngestState, ParseFailure
from mimir.parser import ParsedArticle, normalize_subject, parse_message

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 100
COMMIT_EVERY = 500
DEFAULT_WORKERS = os.cpu_count() or 1
PARSE_CHUNKSIZE = 50


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
        lists=[ArticleList(inbox_id=inbox_id, epoch=epoch, commit_sha=commit_sha)],
    )


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

    logger.info(
        "%s/%s: starting from %s (workers=%d)",
        inbox_name, epoch_name, last_sha or "<beginning>", workers,
    )

    def flush_batch() -> None:
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

        session.add(_to_article(
            parsed, inbox_id=inbox_id, epoch=epoch_name,
            commit_sha=commit_sha, date=commit_time,
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
