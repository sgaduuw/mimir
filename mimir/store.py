from pathlib import Path

from dulwich.repo import Repo
from sqlalchemy import select
from sqlalchemy.orm import Session

from mimir.config import settings
from mimir.models import Article
from mimir.parser import ParsedArticle, parse_message


class MessageNotFound(LookupError):
    pass


def _read_blob(epoch_path: Path, commit_sha: str) -> bytes:
    repo = Repo(str(epoch_path))
    commit = repo[commit_sha.encode()]
    tree = repo[commit.tree]
    _mode, blob_sha = tree[b"m"]
    return repo[blob_sha].data


def read_message(session: Session, message_id: str) -> ParsedArticle:
    """Re-parse a stored article's original RFC 5322 bytes from the git
    mirror. The mirror to read from is resolved via the article's
    list_name → settings.inboxes[list_name].mirror_path."""
    article = session.execute(
        select(Article).where(Article.message_id == message_id)
    ).scalar_one_or_none()
    if article is None:
        raise MessageNotFound(f"no article with message_id={message_id!r}")

    inbox = settings.inboxes.get(article.list_name)
    if inbox is None:
        raise MessageNotFound(
            f"article {message_id!r} is in list {article.list_name!r} "
            f"which is no longer configured"
        )
    epoch_path = inbox.mirror_path / article.epoch
    if not epoch_path.exists():
        raise MessageNotFound(
            f"epoch repo {epoch_path} not found; cannot fetch blob for {message_id!r}"
        )

    raw = _read_blob(epoch_path, article.commit_sha)
    return parse_message(raw)
