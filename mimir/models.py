from datetime import datetime

from sqlalchemy import ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mimir.extensions import Base


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    list_name: Mapped[str] = mapped_column(String, index=True, server_default="lkml")
    epoch: Mapped[str] = mapped_column(String, index=True)
    commit_sha: Mapped[str] = mapped_column(String)
    subject: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    date: Mapped[datetime | None] = mapped_column(index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String, index=True)
    references: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Best-guess parent for threading: in_reply_to OR last entry of references.
    # Computed at ingest in _to_article. Used by mimir.threading CTEs;
    # in_reply_to keeps its raw-header semantics for completeness.
    thread_parent: Mapped[str | None] = mapped_column(String, index=True)
    # Subject with leading reply/forward prefixes stripped, lowercased,
    # whitespace-collapsed. Indexed so we can find sibling orphan threads
    # with matching conversation subjects (JWZ subject-based grouping).
    subject_normalized: Mapped[str] = mapped_column(String, default="", index=True)

    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"))
    filename: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column()

    article: Mapped[Article] = relationship(back_populates="attachments")


class IngestState(Base):
    __tablename__ = "ingest_state"

    list_name: Mapped[str] = mapped_column(String, primary_key=True, server_default="lkml")
    epoch: Mapped[str] = mapped_column(String, primary_key=True)
    last_commit_sha: Mapped[str | None] = mapped_column(String)
    last_ingested_at: Mapped[datetime | None]
