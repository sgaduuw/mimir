from pathlib import Path

from dulwich.repo import Repo
from sqlalchemy import select
from sqlalchemy.orm import Session

from mimir.models import Article, ArticleList, Inbox
from mimir.parser import ParsedArticle, parse_message


class MessageNotFound(LookupError):
    pass


def _read_blob(epoch_path: Path, commit_sha: str) -> bytes:
    # Repo must be context-managed: dulwich keeps packfile descriptors
    # open as long as the Repo object is alive. Without `with`, the
    # FD lives until GC, which on the message-page hot path (every
    # render reopens the repo) builds up an unbounded handle count.
    # Stale `commit_sha` (mirror was rewritten / blob GC'd) makes
    # dulwich raise KeyError; surface that as MessageNotFound so the
    # route returns a clean 410 instead of bubbling to a 500.
    try:
        with Repo(str(epoch_path)) as repo:
            commit = repo[commit_sha.encode()]
            tree = repo[commit.tree]
            _mode, blob_sha = tree[b"m"]
            return repo[blob_sha].data
    except KeyError as exc:
        raise MessageNotFound(
            f"blob for commit {commit_sha} not found in {epoch_path}: {exc!r}"
        ) from exc


def read_message(
    session: Session, inbox: Inbox, message_id: str
) -> ParsedArticle:
    """Re-parse a stored article's original RFC 5322 bytes from this
    inbox's mirror. Joins articles → article_lists to find the
    (epoch, commit_sha) pointer for THIS inbox (cross-posts have one
    ArticleList row per inbox, possibly with different commit SHAs)."""
    row = session.execute(
        select(ArticleList.epoch, ArticleList.commit_sha)
        .join(Article, Article.id == ArticleList.article_id)
        .where(
            Article.message_id == message_id,
            ArticleList.inbox_id == inbox.id,
        )
    ).one_or_none()
    if row is None:
        raise MessageNotFound(
            f"no article with message_id={message_id!r} in inbox {inbox.name!r}"
        )
    epoch, commit_sha = row

    epoch_path = Path(inbox.mirror_path) / epoch
    if not epoch_path.exists():
        raise MessageNotFound(
            f"epoch repo {epoch_path} not found; cannot fetch blob for {message_id!r}"
        )

    raw = _read_blob(epoch_path, commit_sha)
    return parse_message(raw)
