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
from sqlalchemy import select
from sqlalchemy.orm import Session

from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, Inbox, IngestState
from mimir.parser import ParsedArticle, normalize_subject, parse_message

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 100
COMMIT_EVERY = 500
DEFAULT_WORKERS = os.cpu_count() or 1
PARSE_CHUNKSIZE = 50


class IngestResult(BaseModel):
    epoch: str
    new: int = 0
    linked: int = 0  # article existed (e.g. cross-post from another inbox); only added the link
    skipped: int = 0
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
            result.failed += 1
            continue
        parsed = parsed_or_exc

        if parsed.message_id in seen_in_batch:
            logger.debug("epoch %s commit %s: skip (in-batch dup) %s", epoch_name, commit_sha[:12], parsed.message_id)
            result.skipped += 1
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
                result.skipped += 1
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
                "%s/%s: processed=%d new=%d linked=%d skipped=%d failed=%d",
                inbox_name, epoch_name, processed,
                result.new, result.linked, result.skipped, result.failed,
            )

        if processed % COMMIT_EVERY == 0:
            flush_batch()

    flush_batch()

    result.last_commit_sha = last_seen
    return result


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
                remaining -= r.new + r.skipped + r.failed
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
                remaining -= r.new + r.skipped + r.failed
    return out
