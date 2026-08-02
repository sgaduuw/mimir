from collections.abc import Sequence
from pathlib import Path

from dulwich.repo import Repo
from sqlalchemy import select
from sqlalchemy.orm import Session

from mimir.models import Article, ArticleList, Inbox
from mimir.parser import ParsedArticle, parse_message


class MessageNotFound(LookupError):
    pass


def _blob_bytes(repo: Repo, commit_sha: str) -> bytes:
    """The `m` blob of one commit, from an ALREADY-OPEN repo. Raises
    dulwich's bare KeyError; each caller decides what that means."""
    commit = repo[commit_sha.encode()]
    tree = repo[commit.tree]
    _mode, blob_sha = tree[b"m"]
    return repo[blob_sha].data


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
            return _blob_bytes(repo, commit_sha)
    except KeyError as exc:
        raise MessageNotFound(
            f"blob for commit {commit_sha} not found in {epoch_path}: {exc!r}"
        ) from exc


def read_message(session: Session, inbox: Inbox, message_id: str) -> ParsedArticle:
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


def read_messages(
    session: Session, inbox: Inbox, message_ids: Sequence[str]
) -> dict[str, ParsedArticle]:
    """Bulk sibling of `read_message`: one Repo open per EPOCH rather
    than one per message.

    `Repo(...)` re-reads and re-mmaps the epoch's pack index every
    time. Measured 2026-07-29 against the PRODUCTION mirror, 50
    messages (which was the `thread_view_render_cap` default at the
    time of measurement; it is 75 now) out of one epoch,
    page cache warm, median of 5:

        lkml 9.git        (1.1 GB)   40.6 ms -> 8.7 ms    -79%
        stable-rt 0.git   (1.4 MB)   30.6 ms -> 13.7 ms   -55%
        linux-firmware    (930 MB)   62.1 ms -> 42.7 ms   -31%

    The saving tracks how much of the total is the reopen rather than
    the blob read, not inbox size: linux-firmware's messages carry
    large payloads, so 31% is the floor across the corpus rather than
    the small-inbox case. (Re-measure rather than trusting these; they
    are a floor with a date on them, and the archive only grows.)

    Worth the change on this page specifically: the per-inbox sitemap
    points crawlers straight at it, `public, no-cache` means a fresh
    crawler always pays full price, and the web tier is two sync
    gunicorn workers, so a handful of concurrent worst-case renders
    occupies the whole tier.

    Grouped per epoch rather than one shared handle because a thread
    can straddle an epoch boundary (public-inbox chunks by size, not by
    conversation). In practice that is one open for almost every
    thread.

    Returns only what it could read: a message absent from this inbox,
    from a missing epoch directory, or whose blob the mirror no longer
    has is simply not a key. Callers render such a message without its
    body, exactly as the per-message path's `MessageNotFound` did, so a
    single mirror gap cannot take out the whole conversation. Anything
    else (a corrupt repo, an unparseable message) still propagates,
    matching `read_message`.

    Ceiling: the `IN (...)` is one bind parameter per id against
    SQLite's ~32k limit. Every caller is bounded well below that by
    `thread_view_render_cap`; a genuinely unbounded caller would need
    chunking.
    """
    if not message_ids:
        return {}
    rows = session.execute(
        select(Article.message_id, ArticleList.epoch, ArticleList.commit_sha)
        .join(ArticleList, ArticleList.article_id == Article.id)
        .where(
            Article.message_id.in_(message_ids),
            ArticleList.inbox_id == inbox.id,
        )
    ).all()

    by_epoch: dict[str, list[tuple[str, str]]] = {}
    for message_id, epoch, commit_sha in rows:
        by_epoch.setdefault(epoch, []).append((message_id, commit_sha))

    out: dict[str, ParsedArticle] = {}
    mirror = Path(inbox.mirror_path)
    for epoch, items in by_epoch.items():
        epoch_path = mirror / epoch
        if not epoch_path.exists():
            continue
        with Repo(str(epoch_path)) as repo:
            for message_id, commit_sha in items:
                try:
                    raw = _blob_bytes(repo, commit_sha)
                except KeyError:
                    continue
                out[message_id] = parse_message(raw)
    return out
