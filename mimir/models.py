from datetime import datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mimir.extensions import Base


class Inbox(Base):
    """A public-inbox archive (e.g. lkml, linux-fsdevel). Bootstrapped
    from `Settings.inboxes` (env) and, eventually, managed via an
    admin UI."""
    __tablename__ = "inboxes"

    id: Mapped[int] = mapped_column(primary_key=True)
    # URL slug; matches the dict key in Settings.inboxes.
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    mirror_path: Mapped[str] = mapped_column(String)
    upstream_url: Mapped[str] = mapped_column(String)

    # IngestState rows are tiny (one per epoch, ≤50 total per inbox);
    # safe to lazy-load. ArticleList rows are millions per inbox — no
    # reverse collection on purpose; admin queries should COUNT(*) by
    # inbox_id directly.
    ingest_states: Mapped[list["IngestState"]] = relationship(
        back_populates="inbox", cascade="all, delete-orphan"
    )


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    subject: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    date: Mapped[datetime | None] = mapped_column(index=True)
    # Best-guess parent for threading: in_reply_to OR last entry of
    # references, computed at ingest time. The raw headers are read
    # back from the git blob on demand.
    thread_parent: Mapped[str | None] = mapped_column(String, index=True)
    # Subject with reply/forward prefixes stripped (lowercased,
    # whitespace collapsed) for JWZ-style grouping of orphan threads.
    subject_normalized: Mapped[str] = mapped_column(String, default="", index=True)

    # Cross-posted messages share one Article + multiple ArticleList
    # rows (one per inbox they appeared in).
    lists: Mapped[list["ArticleList"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )


class ArticleList(Base):
    """Per-inbox presence of an Article. (epoch, commit_sha) point at
    the blob in *this* inbox's mirror — different mirrors commit the
    same message under different SHAs."""
    __tablename__ = "article_lists"

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    inbox_id: Mapped[int] = mapped_column(
        ForeignKey("inboxes.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    epoch: Mapped[str] = mapped_column(String)
    commit_sha: Mapped[str] = mapped_column(String)

    article: Mapped[Article] = relationship(back_populates="lists")
    inbox: Mapped[Inbox] = relationship()


class IngestState(Base):
    __tablename__ = "ingest_state"

    inbox_id: Mapped[int] = mapped_column(
        ForeignKey("inboxes.id", ondelete="CASCADE"), primary_key=True
    )
    epoch: Mapped[str] = mapped_column(String, primary_key=True)
    last_commit_sha: Mapped[str | None] = mapped_column(String)

    inbox: Mapped[Inbox] = relationship(back_populates="ingest_states")
