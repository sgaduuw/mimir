from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
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
        String,
        nullable=True,
        index=True,
    )

    # Per-inbox tracker tiles on the dashboard. NULL = no tracker
    # section rendered; a dict of {label: email_substring} drives one
    # tile per entry. Managed via `admin inbox trackers`.
    tracked_authors: Mapped[dict[str, str] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    # Cached "max article date in this inbox", bumped on every
    # successful ingest commit. Exists so the front-page "Last
    # activity" string doesn't ride the 24h `archive_stats` cache
    # row (the slow COUNT(*) is what justifies that TTL; MAX(date)
    # rode along and inherited the same staleness window, see #216).
    # NULL on inboxes that haven't ingested anything yet; otherwise
    # monotonic non-decreasing.
    last_article_date: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    # IngestState rows are tiny (one per epoch, ≤50 total per inbox);
    # safe to lazy-load. ArticleList rows are millions per inbox, no
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

    # Patch-series identity for cover letters (`[PATCH ... 0/N]`
    # subjects). NULL on every non-cover-letter article. Indexed
    # so the timeline render, "v1 (date) → v2 (date) → v3 (this)"
    # , can fetch siblings in one query.
    # `patch_series_key` is a SHA-1 hex digest over
    # (author-address, normalised-title), opaque on purpose so a
    # query or log line doesn't leak the author's email. See
    # `mimir.patch_series.series_key`.
    # `patch_series_version` is a short marker like `v1`, `v2`,
    # `rfc`. The unversioned-but-cover-letter case is materialised
    # as `v1`.
    # `patch_series_position` (#212): NULL = not a series patch;
    # 0 = cover letter; positive = in-series patch position (the
    # `M` of `[PATCH M/T]`). Position is set by the subject parser
    # alone, no cross-article lookup; key + version follow the
    # cover-letter linkage and may lag (NULL until backfill walks
    # the thread parent).
    patch_series_key: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        index=True,
    )
    patch_series_version: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )
    patch_series_position: Mapped[int | None] = mapped_column(
        nullable=True,
    )

    # Author's intended primary list, derived from the first list-shaped
    # address in `To:` (then `Cc:`) at ingest time. NULL when no
    # list-shaped address matched a known inbox, render-time falls back
    # to the alphabetically-first inbox among `lists`. SET NULL on inbox
    # delete so removing an inbox doesn't strand or drop articles.
    canonical_inbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("inboxes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
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

    # Review-attestation trailers extracted from the body (Reviewed-by,
    # Acked-by, Tested-by, ...). Populated at ingest time. Empty for
    # articles with no such trailers. Indexed for cross-reference
    # surfaces (issue #97 slices 2+3: per-subsystem active reviewers,
    # per-author "reviewed by this person").
    trailers: Mapped[list["ArticleTrailer"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )


class ArticleList(Base):
    """Per-inbox presence of an Article. (epoch, commit_sha) point at
    the blob in *this* inbox's mirror, different mirrors commit the
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
    only, see `mimir.cache` for the encoder/decoder."""

    __tablename__ = "cache"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[int] = mapped_column(index=True)


class InboxAddressObservation(Base):
    """Per-(inbox, address) tally of list-shaped addresses observed in
    To/Cc of messages archived in this inbox. Used to auto-promote
    `Inbox.list_address` once an inbox accumulates a clear modal
    address, bootstraps canonical resolution without hardcoding
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
    threads, acceptable trade for high-precision matches."""

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


class ArticleTrailer(Base):
    """One review-attestation trailer (Reviewed-by, Acked-by, Tested-by,
    ...) extracted from a message body. Multiple rows per article when
    a patch carries several trailers; an own primary key (rather than a
    composite (article_id, role, address)) because the same person can
    appear under more than one role on the same patch (Reported-by +
    Tested-by) and we want both rows.

    `address_normalized` is the lowercased address; the original casing
    is preserved in `address` for display fidelity. Indexed on
    `(role, address_normalized)` so the per-author "Reviewed by this
    person" query (slice 3) is a tight index scan, and on `article_id`
    for the per-message lookup the cascade already implies.

    Redaction is a render-time concern, not a storage one: the address
    is stored verbatim and the allowlist is consulted only when
    rendering. See CONTEXT.md "Redaction is a display-time decision"."""

    __tablename__ = "article_trailers"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        index=True,
    )
    # Canonical capitalisation per `parser.INDEXED_TRAILER_ROLES`
    # (e.g. "Reviewed-by"). The body's original casing is lost here;
    # the rendered trailer in the message body keeps it.
    role: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String, default="")
    address: Mapped[str] = mapped_column(String)
    address_normalized: Mapped[str] = mapped_column(String)

    article: Mapped[Article] = relationship(back_populates="trailers")

    __table_args__ = (
        # Per-person reverse lookup ("everything Reviewed-by alice@x"):
        # role first because slice 3 always knows the role from the URL
        # ("/reviewers/<addr>" filters role IN (Reviewed-by, Acked-by, …)),
        # then equality on address_normalized.
        Index(
            "ix_article_trailers_role_addr",
            "role",
            "address_normalized",
        ),
    )


class Subsystem(Base):
    """One MAINTAINERS section (e.g. "BCACHEFS"). Replaced wholesale
    on every `update-mainline` tick, the upstream file is the
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
    (trailing slash, brace expansion, etc.), interpretation lives
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
    address (MAINTAINERS' `L:` field) lives elsewhere, it's per-
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
    - `last_commit_sha`, HEAD at the last MAINTAINERS load. Lets
      `update-mainline` skip the parse step when HEAD hasn't moved.
    - `commits_walked_to_sha`, the most recent commit the
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
        String,
        nullable=True,
    )
    last_walked_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )


class MainlineCommit(Base):
    """One (commit, referenced message-id) pair extracted from a
    `Link: https://lore.kernel.org/.../<msgid>` trailer in a
    mainline-tree commit message.

    Composite PK so a commit can carry multiple `Link:` trailers
    (rare but legal, a fix referencing two prior reports, say).
    `message_id` is indexed because the patch-page lookup is "given
    this article's message_id, do we have any commits that applied
    it?", the read of record.

    `tree_name` is indexed so the future stable / next surfaces can
    filter cheaply. For now there's one tree (`linus`).

    `committed_at` is the commit's commit-time in UTC, what we
    render as "Applied as <sha> on <date>" on the patch page.
    """

    __tablename__ = "mainline_commits"

    commit_sha: Mapped[str] = mapped_column(String, primary_key=True)
    message_id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        index=True,
    )
    tree_name: Mapped[str] = mapped_column(String, index=True)
    committed_at: Mapped[datetime] = mapped_column()


class RobotsRule(Base):
    """One row per `User-agent:` stanza in the rendered `/robots.txt`.
    The `*` stanza is the structural default, seeded from
    `mimir.robots._DEFAULT_STAR_DISALLOW` (crawl-delay plus the
    attachment / htmx-partial / internal-search disallows). Per-bot
    rows added via
    `admin robots add <ua>` produce additional stanzas (e.g. a
    `User-agent: GPTBot` block).

    `disallow_paths` is a JSON list of strings; each renders as one
    `Disallow:` line. NULL or `[]` plus NULL `crawl_delay` plus NULL
    `content_signals` is a no-op row (skipped at render time so a row
    never produces a stanza with only `User-agent:` and nothing else).

    `content_signals` is a JSON dict of `key: "yes" | "no"` over the
    Cloudflare-proposed Content-Signal keys (`search`, `ai-input`,
    `ai-train`). Rendered as one `Content-Signal: k=v, k=v` line
    between `User-agent:` and `Crawl-delay:`/`Disallow:`. Operators
    set per stanza via `admin robots {add,update} <ua>
    --content-signal …`.
    """

    __tablename__ = "robots_rules"

    user_agent: Mapped[str] = mapped_column(String, primary_key=True)
    crawl_delay: Mapped[int | None] = mapped_column(nullable=True)
    disallow_paths: Mapped[list[str] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    content_signals: Mapped[dict[str, str] | None] = mapped_column(
        JSON,
        nullable=True,
    )


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
