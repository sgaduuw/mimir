from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String, Text
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

    # The list address as it appears in To/Cc on this inbox's messages
    # (e.g. "linux-fsdevel@vger.kernel.org"). Used to resolve canonical
    # inbox for cross-posted articles. Auto-detected from observed
    # message headers once an inbox crosses the promotion threshold;
    # operator can override via the admin CLI for non-standard lists.
    list_address: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True,
    )

    # Per-inbox tracker tiles on the dashboard. NULL = no tracker
    # section rendered; a dict of {label: email_substring} drives one
    # tile per entry. Managed via `admin inbox trackers`.
    tracked_authors: Mapped[dict[str, str] | None] = mapped_column(
        JSON, nullable=True,
    )

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

    # Author's intended primary list, derived from the first list-shaped
    # address in `To:` (then `Cc:`) at ingest time. NULL when no
    # list-shaped address matched a known inbox — render-time falls back
    # to the alphabetically-first inbox among `lists`. SET NULL on inbox
    # delete so removing an inbox doesn't strand or drop articles.
    canonical_inbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("inboxes.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    canonical_inbox: Mapped["Inbox | None"] = relationship(
        foreign_keys=[canonical_inbox_id],
    )

    # Cross-posted messages share one Article + multiple ArticleList
    # rows (one per inbox they appeared in).
    lists: Mapped[list["ArticleList"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )

    # Diff-touched paths for articles whose body parses as a patch.
    # Populated at ingest time only when extraction finds any
    # `diff --git` headers; non-patch articles have zero rows here.
    # Used by the patch-page subsystem header and the "other recent
    # patches touching X" sidebar (issue #67 slices 2+3).
    files: Mapped[list["ArticleFile"]] = relationship(
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


class CacheEntry(Base):
    """Cross-process cache for slow dashboard queries. JSON values
    only — see `mimir.cache` for the encoder/decoder."""
    __tablename__ = "cache"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[int] = mapped_column(index=True)


class InboxAddressObservation(Base):
    """Per-(inbox, address) tally of list-shaped addresses observed in
    To/Cc of messages archived in this inbox. Used to auto-promote
    `Inbox.list_address` once an inbox accumulates a clear modal
    address — bootstraps canonical resolution without hardcoding
    name→address mappings.

    Conservative filter (`canonical.is_list_address`) keeps personal
    addresses, vendor auto-replies, and bot accounts out of the tally.
    """
    __tablename__ = "inbox_address_observations"

    inbox_id: Mapped[int] = mapped_column(
        ForeignKey("inboxes.id", ondelete="CASCADE"), primary_key=True
    )
    address: Mapped[str] = mapped_column(String, primary_key=True)
    count: Mapped[int] = mapped_column(default=0)
    last_seen: Mapped[datetime] = mapped_column()


class ArticleFile(Base):
    """One (article, file-path) pair extracted from a patch body's
    `diff --git a/<path> b/<path>` headers. Composite PK so each
    pair is unique; backfill / re-ingest stays idempotent without
    a UNIQUE constraint song-and-dance.

    Why only diffs and not free-text path mentions in cover letters:
    `diff --git` is a strong signal (machine-generated, unambiguous),
    whereas prose-shaped mentions like "we should touch fs/foo/" are
    high-noise. The cost of missing those is that the "other
    patches touching X" sidebar under-represents discussion-only
    threads — acceptable trade for high-precision matches."""
    __tablename__ = "article_files"

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    # Path as it appears after `b/` in the `diff --git` line. We
    # store the literal string; downstream glob-matching against
    # MAINTAINERS' `F:` patterns interprets directory semantics.
    # Indexed for the "other patches touching <path>" reverse query.
    path: Mapped[str] = mapped_column(String, primary_key=True, index=True)

    article: Mapped[Article] = relationship(back_populates="files")


class Subsystem(Base):
    """One MAINTAINERS section (e.g. "BCACHEFS"). Replaced wholesale
    on every `update-mainline` tick — the upstream file is the
    source of truth, mimir's table is a cached projection. The CLI
    runs `DELETE FROM subsystems` (cascades to paths + maintainers)
    then re-inserts; idempotent and avoids drift if the upstream file
    renames or removes entries.

    `name` is the section title verbatim, which means uppercase ASCII
    in current usage but the schema permits arbitrary Unicode. `status`
    is the `S:` field (`Supported`, `Maintained`, `Odd Fixes`, `Orphan`,
    `Obsolete`); NULL for sections that omit it (rare but legal)."""
    __tablename__ = "subsystems"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)

    paths: Mapped[list["SubsystemPath"]] = relationship(
        back_populates="subsystem", cascade="all, delete-orphan"
    )
    maintainers: Mapped[list["SubsystemMaintainer"]] = relationship(
        back_populates="subsystem", cascade="all, delete-orphan"
    )


class SubsystemPath(Base):
    """One `F:` (include) or `X:` (exclude) glob from a MAINTAINERS
    section. The literal MAINTAINERS-shaped string is stored
    (trailing slash, brace expansion, etc.) — interpretation lives
    in the future glob-matcher, not in the schema."""
    __tablename__ = "subsystem_paths"

    id: Mapped[int] = mapped_column(primary_key=True)
    subsystem_id: Mapped[int] = mapped_column(
        ForeignKey("subsystems.id", ondelete="CASCADE"), index=True
    )
    glob: Mapped[str] = mapped_column(String)
    # True for `X:` (exclude) lines, False for `F:` (include).
    is_exclude: Mapped[bool] = mapped_column(default=False)

    subsystem: Mapped[Subsystem] = relationship(back_populates="paths")


class SubsystemMaintainer(Base):
    """One `M:` (maintainer) or `R:` (reviewer) entry. The list
    address (MAINTAINERS' `L:` field) lives elsewhere — it's per-
    subsystem, not per-person, and is captured as
    `Subsystem.lists` in the parser dataclass. (Decided not to
    schema it separately for slice 1; revisit if a downstream
    surface needs the per-section list addresses indexed.)"""
    __tablename__ = "subsystem_maintainers"

    id: Mapped[int] = mapped_column(primary_key=True)
    subsystem_id: Mapped[int] = mapped_column(
        ForeignKey("subsystems.id", ondelete="CASCADE"), index=True
    )
    # `M` or `R`. Indexed because the "maintainers only" filter on a
    # subsystem page is the common read.
    role: Mapped[str] = mapped_column(String, index=True)
    # Display name from the entry; empty string when the source had a
    # bare address (rare but legal).
    name: Mapped[str] = mapped_column(String, default="")
    address: Mapped[str] = mapped_column(String, index=True)

    subsystem: Mapped[Subsystem] = relationship(back_populates="maintainers")


class MainlineState(Base):
    """Tracks which version of the mainline tree (Linus's `linux.git`)
    we last interacted with. One row keyed by tree_name so the
    schema permits future mirroring of multiple trees (linux-stable,
    linux-next) without a migration.

    Two independent cursors:
    - `last_commit_sha` — HEAD at the last MAINTAINERS load. Lets
      `update-mainline` skip the parse step when HEAD hasn't moved.
    - `commits_walked_to_sha` — the most recent commit the
      `Link:`-trailer walker has processed. Independent of the
      MAINTAINERS cursor because MAINTAINERS only changes when
      that one file does, but the commit walker has new work on
      almost every tick. Walker is incremental: the next run
      starts after this SHA.
    """
    __tablename__ = "mainline_state"

    tree_name: Mapped[str] = mapped_column(String, primary_key=True)
    last_commit_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    commits_walked_to_sha: Mapped[str | None] = mapped_column(
        String, nullable=True,
    )


class MainlineCommit(Base):
    """One (commit, referenced message-id) pair extracted from a
    `Link: https://lore.kernel.org/.../<msgid>` trailer in a
    mainline-tree commit message.

    Composite PK so a commit can carry multiple `Link:` trailers
    (rare but legal — a fix referencing two prior reports, say).
    `message_id` is indexed because the patch-page lookup is "given
    this article's message_id, do we have any commits that applied
    it?" — the read of record.

    `tree_name` is indexed so the future stable / next surfaces can
    filter cheaply. For now there's one tree (`linus`).

    `committed_at` is the commit's commit-time in UTC — what we
    render as "Applied as <sha> on <date>" on the patch page.
    """
    __tablename__ = "mainline_commits"

    commit_sha: Mapped[str] = mapped_column(String, primary_key=True)
    message_id: Mapped[str] = mapped_column(
        String, primary_key=True, index=True,
    )
    tree_name: Mapped[str] = mapped_column(String, index=True)
    committed_at: Mapped[datetime] = mapped_column()


class ParseFailure(Base):
    """One row per (inbox, epoch, commit_sha) whose `m` blob couldn't
    be parsed. Persisted so the operator can enumerate them and replay
    after a parser fix instead of scanning ingest logs.

    Cleared automatically when the same commit later parses cleanly
    (via `flask --app mimir admin failures replay` or a re-walk).
    """
    __tablename__ = "parse_failures"

    inbox_id: Mapped[int] = mapped_column(
        ForeignKey("inboxes.id", ondelete="CASCADE"), primary_key=True
    )
    epoch: Mapped[str] = mapped_column(String, primary_key=True)
    commit_sha: Mapped[str] = mapped_column(String, primary_key=True)
    # Exception type name (e.g. "MessageTooLarge", "ValueError"). Indexed
    # so `failures list --error-class X` is cheap even at 10k+ rows.
    error_class: Mapped[str] = mapped_column(String, index=True)
    error_message: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column()
    last_attempt: Mapped[datetime] = mapped_column()
    attempts: Mapped[int] = mapped_column(default=1)
