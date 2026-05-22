"""Re-parse persisted `parse_failures` rows after a parser fix.

`ingest_epoch` records every parse failure as a row in `parse_failures`;
this module re-walks them sequentially. On success: insert the Article
(or cross-post link) and delete the row. On failure: bump
attempts/last_attempt and refresh the error fields. Used by both the
`admin failures replay` CLI command and any operator-driven cleanup
after a parser change.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from dulwich.errors import NotGitRepository
from dulwich.repo import Repo
from pydantic import BaseModel
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.ingest.epoch import _to_article
from mimir.models import (
    Article,
    ArticleList,
    Inbox,
    ParseFailure,
)
from mimir.parser import parse_message

logger = logging.getLogger(__name__)


class ReplayResult(BaseModel):
    """Outcome of replaying persisted parse failures for one inbox."""

    attempted: int = 0
    recovered: int = 0  # parsed cleanly; row deleted, article inserted/linked.
    still_failed: int = 0  # parse still raises; row's last_attempt + attempts updated.
    skipped: int = 0  # blob couldn't be fetched (mirror missing, ref pruned).


def replay_failures(
    inbox: Inbox,
    epoch_filter: str | None = None,
    limit: int | None = None,
) -> ReplayResult:
    """Re-parse persisted parse_failures rows for `inbox`.

    On success: insert the Article (or cross-post link) and delete the
    failure row. On failure: bump attempts/last_attempt and refresh the
    error fields. Sequential by design, replay is a low-volume admin
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

        # Group by epoch so we open each dulwich repo once. Close
        # each cached repo before returning: dulwich's `Repo` holds
        # FDs on pack files, refs, and the loose-object dir, and has
        # no `__del__`, so the FDs leak until the dict gets GC'd.
        repo_cache: dict[str, Repo] = {}
        try:
            for row in rows:
                out.attempted += 1
                repo_path = Path(attached.mirror_path) / row.epoch
                repo = repo_cache.get(row.epoch)
                if repo is None:
                    try:
                        repo = Repo(str(repo_path))
                    except NotGitRepository, FileNotFoundError:
                        out.skipped += 1
                        continue
                    repo_cache[row.epoch] = repo

                try:
                    commit = repo[row.commit_sha.encode()]
                    tree = repo[commit.tree]
                    _mode, blob_sha = tree[b"m"]
                    raw = repo[blob_sha].data
                    commit_time = datetime.fromtimestamp(
                        commit.commit_time, timezone.utc
                    )
                except KeyError:
                    # Commit or `m` blob missing, mirror was pruned or
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
                    session.add(
                        _to_article(
                            parsed,
                            inbox_id=attached.id,
                            epoch=row.epoch,
                            commit_sha=row.commit_sha,
                            date=commit_time,
                            session=session,
                        )
                    )
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
                        session.add(
                            ArticleList(
                                article_id=existing_id,
                                inbox_id=attached.id,
                                epoch=row.epoch,
                                commit_sha=row.commit_sha,
                            )
                        )
                session.delete(row)
                out.recovered += 1
            session.commit()
        finally:
            for repo in repo_cache.values():
                repo.close()
    return out
