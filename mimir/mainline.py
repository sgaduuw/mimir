"""Mainline-tree indexing: end-to-end ownership of the mainline
tree's two derived surfaces.

Two sub-areas, one concern (the mainline tree, end to end):

- **Walker.** `extract_message_ids` + `walk_commits` scan commit
  messages for `Link: https://lore.kernel.org/.../<msgid>` trailers
  and write them to `mainline_commits` so a patch page can render
  "Applied as `<sha>` on <date>".
- **Orchestration.** `update_mainline` is the operator-facing entry
  point: optionally `git fetch`, reload MAINTAINERS, walk for
  trailers. Called by the `mimir update-mainline` CLI and the
  Phase 2.3 broker handler.

Splitting orchestration off into its own module would scatter
state coupled to the same tree (the `MainlineState` row, the
`tree_path` resolution, the trailer regex) and force every caller
to learn two paths. Keeping it together follows the same shape as
`mimir/inboxes.py` (bootstrap + CRUD + observe in one place).
"""

import logging
import re
import subprocess
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from dulwich.repo import Repo
from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from mimir import maintainers
from mimir.broker.writes import WriteFuture, WriteOp
from mimir.config import settings
from mimir.datetime_utils import aware_utc

if TYPE_CHECKING:
    from mimir.config import TreeConfig
from mimir.extensions import SessionLocal
from mimir.models import (
    MainlineCommit,
    MainlineState,
    Subsystem,
    SubsystemMaintainer,
    SubsystemPath,
)

logger = logging.getLogger(__name__)


# `Link: https://lore.kernel.org/(slug/)?<msgid>(/)?`
#
# Variations seen in real Linus-tree commits:
#   Link: https://lore.kernel.org/r/aNU-FkJEcA3T4aDB@intel.com
#   Link: https://lore.kernel.org/175852292275...@devnote2
#   Link: https://lore.kernel.org/all/175824455687...@devnote2/
#
# The optional slug is `r`, `all`, `lkml`, `linux-fsdevel`, etc.
# We accept any `[a-z][a-z0-9-]*` segment before the msgid; the
# msgid itself is `<localpart>@<domainpart>` with no whitespace
# or path separators in either half.
_LINK_RE = re.compile(
    r"^Link:\s+https?://lore\.kernel\.org/"
    r"(?:[a-z][a-z0-9-]*/)?"
    r"([^/\s]+@[^/\s]+?)"
    r"/?\s*$",
    re.MULTILINE,
)


def extract_message_ids(commit_message: bytes) -> list[str]:
    """Return every `lore.kernel.org` msgid referenced by `Link:`
    trailers in this commit message, in source-file order, deduped.
    Returns `[]` when no trailers match. Decoding is UTF-8 with
    surrogate-escape so a non-decodable byte in a commit message
    doesn't crash the walker.

    Real-world commits sometimes carry the same Message-ID twice
    (e.g. one Link with the `/r/` slug and another with `/all/`, or
    a stable-cherry-pick that copies the original trailer alongside
    an Upstream: line that's itself a Link). Without dedup those
    duplicates land as parallel insert rows and trip the UNIQUE
    `(commit_sha, message_id)` constraint, aborting the whole
    batch, observed against linux.git commit 9e8e8912b05f."""
    text = commit_message.decode("utf-8", errors="surrogateescape")
    seen: set[str] = set()
    out: list[str] = []
    for m in _LINK_RE.finditer(text):
        mid = m.group(1)
        if mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


class WalkResult(BaseModel):
    """Outcome counters for one `walk_commits` invocation."""

    commits_seen: int = 0
    # Commits with at least one extractable `Link:` trailer.
    linked: int = 0
    # Rows written (a commit with N trailers contributes N rows).
    rows_inserted: int = 0
    last_walked_sha: str | None = None


# Page size for batched commits. Each batch commits to SQLite and
# resumes, a Ctrl-C mid-walk loses at most this many commits of
# progress but no DB corruption.
_BATCH = 500


def walk_commits(
    session: Session,
    tree_path: Path,
    tree_name: str = "linus",
    *,
    writer,
    batch_size: int = 100,
    branch: str = "HEAD",
    rebases: bool = False,
    exclude_from: bytes | None = None,
) -> WalkResult:
    """Walk new commits on `tree_path`, extract `Link:` trailers,
    insert `mainline_commits` rows.

    Phase 3 of the two-pool restructure
    (`_claude/specs/2026-05-29-broker-two-pool-design.md`): writes
    dispatch through `writer` (a `WriterThread`) via
    `_submit_mainline_batch` and `_submit_mainline_cursor_update`.
    `batch_size` is operator-tunable via
    `settings.mainline_commit_batch_size` (default 100); each
    batch's future is waited before composing the next batch so
    the writer lock hold per batch is short (~30-80 ms) rather
    than one continuous multi-minute transaction.

    `session` is a query_only SQLAlchemy session from the active
    ReadSessionPool; it is used for reads only (MainlineState fetch,
    branch-ref resolution) and must not be committed for writes here.

    When `rebases=True`: a WriteOp deletes the tree's existing rows
    first, then the full walk re-inserts; INSERT-OR-IGNORE absorbs
    intra-walk duplicates.

    When `rebases=False` (default): incremental SHA-cursor walk. The
    cursor on `MainlineState.commits_walked_to_sha` is advanced past
    the last seen SHA so the next tick only sees commits since the
    prior run. Missing-cursor defensive rewalk stays in place.

    `branch` selects which ref to walk. Defaults to HEAD; override
    for trees like akpm/mm where the canonical pickup signal lives on
    a non-HEAD branch (e.g. `mm-stable`). Missing branch falls back
    to HEAD with a warning log.

    `exclude_from` (bytes SHA of another tree's HEAD): when set AND
    `rebases=False` AND the SHA is reachable in this repo's
    objectstore (typically via `--reference linus.git`'s alternates),
    the walker also excludes commits reachable from this SHA. The
    intended use is `linus_head`, so non-Linus trees walk only
    commits divergent from Linus instead of the whole shared history.
    Pure perf win on first walks; on subsequent cursor-based walks
    the combined exclude is at least as tight as the cursor alone.

    Not applied for `rebases=True` trees: the daily DELETE+rewalk
    would erase intermediate-tree rows for patches that transition
    into Linus that same day, losing per-tree timeline info on the
    patch page. linux-next's daily O(history) walk is the cost of
    preserving the journey for newly-landed patches.

    Known trade-off (rebases=False): a patch picked up by Linus first
    and back-merged into a subsystem tree later (e.g. a backport
    into a -stable lane) won't get a row for the subsystem tree
    because `exclude_from` blocks the back-merged commit. Acceptable
    for the curated subsystem-tree set where forward flow dominates;
    operators adding a stable / longterm tree should consider this.

    First-run behaviour (rebases=False, no exclude_from): walks the
    entire history. On a full Linus tree that's ~1.5M commits and
    takes minutes. Operator can scope the initial run by setting
    `commits_walked_to_sha` manually (e.g. to a recent release tag)
    if they only care about recent history."""
    with Repo(str(tree_path)) as repo:
        state = session.get(MainlineState, tree_name)
        if state is None:
            state = MainlineState(tree_name=tree_name)
            session.add(state)

        # Resolve the branch ref. HEAD is the common case; named branches
        # are used for trees like akpm/mm where the canonical work lives
        # on a non-default ref. A missing named branch degrades gracefully
        # to HEAD rather than crashing, because a tree config error should
        # not halt other trees' indexing on the same tick. An empty repo
        # (no commits at all) produces a WalkResult with zero commits.
        branch_ref: bytes | None
        if branch == "HEAD":
            try:
                branch_ref = repo.head()
            except KeyError:
                branch_ref = None
        else:
            try:
                branch_ref = repo.refs[f"refs/heads/{branch}".encode()]
            except KeyError:
                logger.warning(
                    "mainline: branch %s missing in %s; falling back to HEAD",
                    branch,
                    tree_path,
                )
                try:
                    branch_ref = repo.head()
                except KeyError:
                    branch_ref = None

        if branch_ref is None:
            # Empty repo or missing ref: nothing to walk. The read-only
            # session has no pending writes so no commit needed; return
            # early with zero counts. Note: the rebases=True DELETE WriteOp
            # has not been submitted yet at this point; that block comes
            # after this guard.
            return WalkResult()

        exclude: list[bytes] = []
        if rebases:
            # Force-pushed tree: wipe the tree's existing rows so stale
            # SHAs don't accumulate. The full walk that follows re-inserts
            # the live history; ON CONFLICT DO NOTHING absorbs any
            # intra-walk duplicates from merge-graph re-emissions.
            # `exclude_from` intentionally NOT applied here; see the
            # docstring for the per-tree-timeline rationale.
            #
            # Phase 3: the DELETE becomes a WriteOp dispatched through
            # the writer, matching every other write in this function.
            def _delete_existing(conn, tn=tree_name):
                conn.execute(
                    delete(MainlineCommit).where(MainlineCommit.tree_name == tn)
                )

            writer.submit(
                WriteOp(
                    label=f"mainline:{tree_name}:delete-rebases", fn=_delete_existing
                ),
            ).result(timeout=60)
        else:
            # Incremental cursor walk. Skip commits already recorded in
            # the prior run by excluding the last-seen SHA. If the cursor
            # SHA no longer exists (force-push, shallow re-clone), re-walk
            # from scratch: we'd rather re-insert against ON CONFLICT than
            # crash and stall the tree.
            since = state.commits_walked_to_sha
            if since:
                try:
                    repo[since.encode()]
                    exclude.append(since.encode())
                except KeyError:
                    logger.warning(
                        "mainline: cursor SHA %s missing from %s; rewalking",
                        since,
                        tree_path,
                    )
            # Additional shortcut: skip commits reachable from another
            # tree's head when supplied (typically `linus_head` so non-
            # Linus trees walk only divergent commits). KeyError means
            # this repo's objectstore doesn't have the SHA, e.g. no
            # --reference linus.git on clone; falling back to a full
            # walk is the right safety net (slower, still correct).
            if exclude_from is not None:
                try:
                    repo[exclude_from]
                    exclude.append(exclude_from)
                except KeyError:
                    logger.debug(
                        "mainline: exclude_from %s not in %s; walking full history",
                        exclude_from.decode("ascii", errors="replace"),
                        tree_path,
                    )

        # dulwich's reverse=True walker emits oldest-first, which is what
        # we want: advance the SHA cursor monotonically so the next tick
        # only picks up commits appended after this one.
        walker = repo.get_walker(include=[branch_ref], exclude=exclude, reverse=True)

        result = WalkResult(
            last_walked_sha=None if rebases else state.commits_walked_to_sha
        )
        pending_rows: list[dict] = []
        # Tracks the SHA of the last commit processed; updated per commit
        # in the main loop and used for the post-walk cursor WriteOp.
        last_seen_sha_for_cursor: str | None = result.last_walked_sha

        def flush() -> None:
            """Submit the current pending_rows batch via the writer and
            wait for the commit before clearing the buffer.

            Phase 3: writes go through _submit_mainline_batch rather than
            inline session.execute + session.commit. The cursor update is
            deferred to the post-walk submission so the cursor only
            advances after ALL batches for this tree have committed."""
            nonlocal pending_rows
            if not pending_rows:
                return
            # _submit_mainline_batch handles the INSERT OR IGNORE for the
            # three dup scenarios described in the helper's docstring. Wait
            # on the future so we don't compose the next batch until this
            # one has committed: preserves the resume-from-cursor invariant
            # (last_seen_sha_for_cursor only advances after this batch is
            # durable).
            _submit_mainline_batch(writer, tree_name, pending_rows).result(timeout=60)
            pending_rows = []

        for entry in walker:
            commit = entry.commit
            sha = commit.id.decode("ascii")
            result.commits_seen += 1
            last_seen_sha_for_cursor = sha

            msgids = extract_message_ids(commit.message)
            if msgids:
                result.linked += 1
                committed_at = datetime.fromtimestamp(
                    commit.commit_time,
                    tz=timezone.utc,
                )
                for mid in msgids:
                    pending_rows.append(
                        {
                            "commit_sha": sha,
                            "message_id": mid,
                            "tree_name": tree_name,
                            "committed_at": committed_at,
                        }
                    )
                    result.rows_inserted += 1

            if result.commits_seen % batch_size == 0:
                flush()
                logger.info(
                    "mainline: walked %d commits (%d linked, %d rows) on %s",
                    result.commits_seen,
                    result.linked,
                    result.rows_inserted,
                    tree_name,
                )

        # Tail flush: submit any remaining rows that didn't fill a complete
        # batch.
        flush()

        # Cursor update: submit the final WriteOp advancing
        # MainlineState.commits_walked_to_sha to the last commit seen. This
        # is the FINAL WriteOp per tree; submitting it after all batches
        # have committed preserves resume semantics: a crash between the
        # last batch and the cursor leaves the cursor at the pre-walk value,
        # so the next tick re-walks the just-inserted commits idempotently
        # via on_conflict_do_nothing.
        #
        # Only submit when we actually advanced (last_seen_sha_for_cursor
        # differs from the initial value stored in result.last_walked_sha):
        # - rebases=True, no commits: both are None (skip).
        # - rebases=True, walked N commits: result.last_walked_sha is None,
        #   last_seen_sha_for_cursor is the final SHA (submit).
        # - rebases=False, no new commits: both equal the old cursor (skip).
        # - rebases=False, walked N commits: they differ (submit).
        if last_seen_sha_for_cursor is not None and (
            last_seen_sha_for_cursor != result.last_walked_sha
        ):
            _submit_mainline_cursor_update(
                writer, tree_name, last_seen_sha_for_cursor
            ).result(timeout=60)

        return result


class TreeWalkResult(BaseModel):
    """Per-tree outcome of one update_mainline tick."""

    skipped: bool = False  # walk_every_seconds not yet elapsed
    ok: bool = True  # False when an exception was caught
    error: str | None = None
    commits_seen: int = 0
    commits_linked: int = 0
    rows_inserted: int = 0


class UpdateMainlineResult(BaseModel):
    """Aggregate outcome of one `update_mainline` invocation across
    every configured tree. Existing fields (`maintainers_ran`,
    `subsystems_loaded`, `mainline_head`, etc.) survive for the
    Linus-only MAINTAINERS reparse path. Per-tree details live in
    `trees`."""

    maintainers_ran: bool = False
    maintainers_unchanged: bool = False
    subsystems_loaded: int = 0
    mainline_head: str | None = None

    commits_ran: bool = False
    commits_seen: int = 0
    commits_linked: int = 0
    rows_inserted: int = 0

    trees: dict[str, TreeWalkResult] = Field(default_factory=dict)


def _ensure_tree(
    tree: "TreeConfig",
    *,
    reference: Path | None,
    skip_fetch: bool,
) -> Path:
    """Clone the tree on first run, fetch otherwise. Returns the
    resolved absolute tree path.

    `reference`: when set (typically the Linus tree path for every
    non-Linus tree), pass `--reference <reference>` to git clone so
    objects are shared across alternates. Marginal disk per non-Linus
    tree drops from ~10 GB to the divergent objects only. Caveat
    documented in CONTEXT.md: a corrupted reference clone breaks every
    dependent tree; recovery is re-cloning without --reference.

    `skip_fetch`: when True on an existing clone, no `git fetch`. CLI
    option for the operator who wants to re-run MAINTAINERS / Link-
    trailer passes against current HEAD without a network round-trip.
    """
    tree_path = tree.path
    if not tree_path.is_absolute():
        # Late import: `PROJECT_ROOT` is a config-module constant but
        # importing it at module load creates a circular dependency with
        # the `Settings` instance.
        from mimir.config import PROJECT_ROOT

        tree_path = PROJECT_ROOT / tree_path

    if not tree_path.exists():
        tree_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--mirror"]
        if reference is not None:
            cmd.extend(["--reference", str(reference)])
        # `--` stops git from interpreting an option-shaped URL as a
        # flag (same defensive pattern as `mimir.sync.clone_epoch`).
        cmd.extend(["--", tree.url, str(tree_path)])
        logger.info("mainline: cloning %s -> %s", tree.url, tree_path)
        subprocess.run(cmd, check=True)
        return tree_path

    if not skip_fetch:
        logger.debug("mainline: fetching %s", tree_path)
        subprocess.run(
            ["git", "-C", str(tree_path), "fetch", "--quiet", "--prune"],
            check=True,
        )
    return tree_path


def _submit_mainline_batch(writer, tree_name: str, batch: list[dict]) -> WriteFuture:
    """Phase 3 of the two-pool restructure
    (`_claude/specs/2026-05-29-broker-two-pool-design.md`).

    Compose a WriteOp wrapping INSERT OR IGNORE for a batch of
    mainline commits and submit it to the writer. Returns the
    WriteFuture so the caller can wait on the commit before
    composing the next batch (preserves resume-from-cursor
    semantics: the cursor only advances after a batch's submitted
    future resolves).

    `batch` is a list of dicts matching the `mainline_commits`
    schema: `commit_sha`, `message_id`, `tree_name`, `committed_at`.
    Per-batch size is operator-tunable via
    `settings.mainline_commit_batch_size`.

    Empty batches are no-ops: return a pre-resolved future so
    the caller's `.result()` doesn't block."""
    if not batch:
        f: Future = Future()
        f.set_result(None)
        return f

    def _fn(conn):
        # Mirror the inline path's SQL exactly so rows written here
        # are indistinguishable from rows written by the existing
        # walk_commits flush() closure (which we're displacing per
        # Phase 3). Composite PK is (commit_sha, message_id);
        # on_conflict_do_nothing handles the three dup scenarios
        # documented in the inline path's comment.
        stmt = (
            sqlite_insert(MainlineCommit)
            .values(batch)
            .on_conflict_do_nothing(index_elements=["commit_sha", "message_id"])
        )
        conn.execute(stmt)

    return writer.submit(
        WriteOp(label=f"mainline:{tree_name}:batch", fn=_fn),
    )


def _submit_mainline_cursor_update(
    writer,
    tree_name: str,
    last_commit_sha: str,
) -> WriteFuture:
    """Phase 3 of the two-pool restructure
    (`_claude/specs/2026-05-29-broker-two-pool-design.md`).

    Compose a WriteOp upserting the Link-trailer walker cursor
    (`MainlineState.commits_walked_to_sha`) for one tree, submit
    to the writer, return the future. Called AFTER all per-tree
    batch WriteOps have committed (their futures resolved); the
    cursor is the final WriteOp per tree so a crash between the
    last batch and the cursor leaves the next tick re-walking
    the just-inserted commits, which is idempotent because
    mainline_commits uses on_conflict_do_nothing on the
    (commit_sha, message_id) PK.

    UPSERT: works whether MainlineState has a row for this tree
    yet or not. tree_name is the PK; on conflict, only
    commits_walked_to_sha is updated. last_commit_sha (the
    MAINTAINERS HEAD cursor) and last_walked_at are not touched
    here; they have their own write paths."""

    def _fn(conn):
        stmt = (
            sqlite_insert(MainlineState)
            .values(
                tree_name=tree_name,
                commits_walked_to_sha=last_commit_sha,
            )
            .on_conflict_do_update(
                index_elements=["tree_name"],
                set_={"commits_walked_to_sha": last_commit_sha},
            )
        )
        conn.execute(stmt)

    return writer.submit(
        WriteOp(label=f"mainline:{tree_name}:cursor", fn=_fn),
    )


def _submit_last_walked_at(
    writer,
    tree_name: str,
    now: datetime,
) -> WriteFuture:
    """Phase 6a. Compose a WriteOp upserting the per-tree cadence
    timestamp (`MainlineState.last_walked_at`) for one tree, submit
    to the writer, return the future.

    This is the steady-state cadence write the scheduler's
    `update-mainline` tick advances on every successful per-tree
    walk. Before 6a it ran on the shared engine via a plain
    `SessionLocal()` get-or-create, which is a shared-engine writer
    reachable from the broker long worker concurrently with the
    WriterThread, defeating the single-writer invariant and staying
    invisible to the slow-write deploy-observe gate (no
    `write_transaction` wrapper).

    UPSERT: works whether MainlineState has a row for this tree yet
    or not. tree_name is the PK; on conflict, ONLY `last_walked_at`
    is updated. `last_commit_sha` (the MAINTAINERS HEAD cursor) and
    `commits_walked_to_sha` (the Link-trailer walker cursor) have
    their own write paths and must not be clobbered here."""

    def _fn(conn):
        stmt = (
            sqlite_insert(MainlineState)
            .values(tree_name=tree_name, last_walked_at=now)
            .on_conflict_do_update(
                index_elements=["tree_name"],
                set_={"last_walked_at": now},
            )
        )
        conn.execute(stmt)

    return writer.submit(
        WriteOp(label=f"update_mainline:last_walked_at:{tree_name}", fn=_fn),
    )


def _submit_maintainers_replace(
    writer,
    tree_name: str,
    head_sha: str,
    force: bool,
    sub_rows: list[dict],
    path_rows_for: Callable[[list[int]], list[dict]],
    maintainer_rows_for: Callable[[list[int]], list[dict]],
    loaded: int,
) -> WriteFuture:
    """Phase 6a. Compose one WriteOp that re-reads MainlineState on the
    writer connection (so the no-op decision and the write are one
    transaction), and, when not a no-op, DELETEs the subsystems triple,
    bulk-inserts the parsed rows wiring path/maintainer FKs to the
    returned subsystem ids, and UPSERTs the cursor. Returns a future
    resolving to (ran: bool, loaded: int).

    The closure mirrors the legacy SessionLocal block's SQL exactly so
    rows are indistinguishable from the pre-6a path. path_rows_for /
    maintainer_rows_for build their row dicts from the RETURNING ids
    (subsystem rows are inserted with sort_by_parameter_order so ids
    align with sub_rows order)."""

    def _fn(conn):
        # Re-read MainlineState on the writer connection so the no-op
        # decision and the replace are one transaction (the read+write
        # atomicity that the legacy write_transaction + SessionLocal
        # block gave). The writer is single-threaded, but keeping it one
        # transaction preserves the BEGIN-IMMEDIATE-shaped invariant.
        row = conn.execute(
            select(MainlineState.last_commit_sha).where(
                MainlineState.tree_name == tree_name
            )
        ).first()
        last_sha = row[0] if row else None
        if last_sha == head_sha and not force:
            # Sentinel: the replace did not run. The caller skips the
            # cache invalidations and returns (False, 0, head_sha).
            return (False, 0)

        # Replace-all in one transaction. The cascade FK on
        # `subsystems.id` clears `subsystem_paths` +
        # `subsystem_maintainers` via ON DELETE CASCADE; the writer
        # connection carries `PRAGMA foreign_keys=ON` (registered via
        # _sqlite_pragmas on the writer engine) so the cascade fires.
        conn.execute(delete(Subsystem))

        # `sort_by_parameter_order=True` on the RETURNING insert aligns
        # the returned ids with the input order so path / maintainer
        # rows wire to the right `subsystem_id` without a name lookup.
        result = conn.execute(
            insert(Subsystem).returning(Subsystem.id),
            sub_rows,
            execution_options={"sort_by_parameter_order": True},
        )
        sub_ids = [r[0] for r in result]

        path_rows = path_rows_for(sub_ids)
        maintainer_rows = maintainer_rows_for(sub_ids)
        if path_rows:
            conn.execute(insert(SubsystemPath), path_rows)
        if maintainer_rows:
            conn.execute(insert(SubsystemMaintainer), maintainer_rows)

        # UPSERT the MAINTAINERS HEAD cursor. Distinct from the
        # Link-trailer walker cursor (`commits_walked_to_sha`); only
        # `last_commit_sha` is touched here.
        conn.execute(
            sqlite_insert(MainlineState)
            .values(tree_name=tree_name, last_commit_sha=head_sha)
            .on_conflict_do_update(
                index_elements=["tree_name"],
                set_={"last_commit_sha": head_sha},
            )
        )
        return (True, loaded)

    return writer.submit(
        WriteOp(label=f"update_mainline:maintainers:{tree_name}", fn=_fn)
    )


def load_maintainers(
    tree_path: Path,
    tree_name: str = "linus",
    *,
    force: bool = False,
) -> tuple[bool, int, str]:
    """Read MAINTAINERS at HEAD and replace the
    `subsystems` / `subsystem_paths` / `subsystem_maintainers`
    triple in one transaction. Returns
    `(ran, subsystems_loaded, head_sha)`; `ran=False` when HEAD
    matches `last_commit_sha` and `force=False`.

    BEGIN IMMEDIATE: the reparse reads `MainlineState` then writes
    the whole subsystems triple in one transaction, the exact
    shape that trips SQLITE_BUSY_SNAPSHOT under concurrent cache
    writes."""
    # Scope the Repo to just the read of MAINTAINERS bytes. The
    # write_transaction block below doesn't need `repo` open, so
    # closing it before the SQL writes releases the pack-file
    # mmaps before the writer lock is acquired.
    with Repo(str(tree_path)) as repo:
        head_sha = repo.head().decode("ascii")
        commit = repo[repo.head()]
        tree = repo[commit.tree]
        try:
            _mode, blob_sha = tree[b"MAINTAINERS"]
        except KeyError as exc:
            raise FileNotFoundError(
                f"no MAINTAINERS file at HEAD of {tree_path}; wrong tree?"
            ) from exc
        blob_bytes = repo[blob_sha].data

    # Phase 6a dual-dispatch: parse + build the row payloads BEFORE the
    # write so the writer-lock hold is short, then either submit one
    # composite WriteOp (in-broker) or fall back to the plain
    # SessionLocal block (startup / non-broker / tests). The closures
    # defer path/maintainer row construction until the RETURNING ids
    # are known so FKs wire to the right subsystem.
    try:
        from mimir.broker import _context

        writer = _context.get_active_writer()
    except RuntimeError:
        writer = None

    parsed = maintainers.parse(blob_bytes)
    sub_rows = [{"name": sub.name, "status": sub.status} for sub in parsed]

    def _path_rows_for(sub_ids: list[int]) -> list[dict]:
        rows: list[dict] = []
        for sub_id, sub in zip(sub_ids, parsed, strict=True):
            for glob in sub.files:
                rows.append({"subsystem_id": sub_id, "glob": glob, "is_exclude": False})
            for glob in sub.excludes:
                rows.append({"subsystem_id": sub_id, "glob": glob, "is_exclude": True})
        return rows

    def _maintainer_rows_for(sub_ids: list[int]) -> list[dict]:
        rows: list[dict] = []
        for sub_id, sub in zip(sub_ids, parsed, strict=True):
            for m in sub.maintainers:
                rows.append(
                    {
                        "subsystem_id": sub_id,
                        "role": m.role,
                        "name": m.name,
                        "address": m.address,
                    }
                )
        return rows

    loaded = len(parsed)

    if writer is not None:
        ran, loaded_out = _submit_maintainers_replace(
            writer,
            tree_name,
            head_sha,
            force,
            sub_rows,
            _path_rows_for,
            _maintainer_rows_for,
            loaded,
        ).result()
        if not ran:
            return False, 0, head_sha
        loaded = loaded_out
    else:
        # Legacy shared-engine fallback (startup pre-serve, non-broker
        # callers, tests). Same SQL as the writer closure. No
        # write_transaction wrapper: removed in Phase 6b now that the
        # broker is the sole writer on its own engine and no concurrent
        # shared-engine committer exists during serving.
        with SessionLocal() as session:
            state = session.get(MainlineState, tree_name)
            if state is None:
                state = MainlineState(tree_name=tree_name)
                session.add(state)
            if state.last_commit_sha == head_sha and not force:
                return False, 0, head_sha
            # The cascade FK on `subsystems.id` clears `subsystem_paths`
            # + `subsystem_maintainers` via ON DELETE CASCADE; SQLite
            # needs `PRAGMA foreign_keys=ON` (set by `mimir.extensions`
            # on every connection) for the cascade to fire.
            session.execute(delete(Subsystem))
            # `sort_by_parameter_order=True` aligns the returned ids with
            # the input order so path / maintainer rows wire to the right
            # `subsystem_id` without a name lookup.
            result = session.execute(
                insert(Subsystem).returning(Subsystem.id),
                sub_rows,
                execution_options={"sort_by_parameter_order": True},
            )
            sub_ids = [row[0] for row in result]
            path_rows = _path_rows_for(sub_ids)
            maintainer_rows = _maintainer_rows_for(sub_ids)
            if path_rows:
                session.execute(insert(SubsystemPath), path_rows)
            if maintainer_rows:
                session.execute(insert(SubsystemMaintainer), maintainer_rows)
            state.last_commit_sha = head_sha
            session.commit()

    # Invalidate the two derived caches that key off MAINTAINERS:
    # the dynamic-allowlist (M:/R: address set, used by From-line +
    # DCO-trailer redaction) and the subsystems-rule snapshot (used
    # by `subsystems_for_article` on every message page). The cache
    # table is shared across processes, so these deletes from the
    # sidecar / broker reach the web container too.
    from mimir import maintainer_allowlist, subsystems

    maintainer_allowlist.invalidate()
    subsystems.invalidate_rules_snapshot()

    return True, loaded, head_sha


def _read_linus_head(linus_disk_path: Path) -> bytes | None:
    """Read Linus's HEAD SHA from disk. Returns None on any error
    (missing clone, unreadable, no HEAD, etc.); callers fall back to
    walking the full history. Repo is context-managed so the brief
    mmap of the pack files releases at function return rather than
    on the next GC pass."""
    try:
        with Repo(str(linus_disk_path)) as repo:
            return repo.head()
    except Exception as exc:
        logger.debug(
            "mainline: could not read linus HEAD from %s: %r",
            linus_disk_path,
            exc,
        )
        return None


def update_mainline(
    *,
    skip_fetch: bool = False,
    skip_maintainers: bool = False,
    skip_commits: bool = False,
    force: bool = False,
) -> UpdateMainlineResult:
    """Sync every configured tree and refresh derived surfaces.

    Iterates `Settings.trees`; per-tree decisions:
      - cadence gate: skip if `now - last_walked_at < walk_every_seconds`
      - linus only: load_maintainers() runs (MAINTAINERS is Linus-only)
      - walker dispatch: rebases=True triggers DELETE+rewalk
      - failure isolation: log + record per-tree, continue

    Per-tree cadence: skip the tree on ticks that arrive sooner than
    `walk_every_seconds` since the last *successful* walk. Failed walks
    do not advance the cursor; the next tick retries. Any combination
    of skip_maintainers / skip_commits is treated as a successful tick
    if `_ensure_tree` succeeds and neither the MAINTAINERS nor commit
    phases raised an exception.
    """
    from datetime import datetime, timezone

    result = UpdateMainlineResult()
    now = datetime.now(timezone.utc)

    linus_path: Path | None = None

    # Read Linus's HEAD up front (if the clone exists on disk) so
    # non-Linus walks can shortcut over commits already reachable
    # from Linus. Reading off-disk works even when Linus is cadence-
    # skipped this tick. On the very first deploy, Linus's clone
    # doesn't exist yet; linus_head stays None and the shortcut is
    # unavailable for that one tick, which is correct (non-Linus
    # trees walk full history just like the pre-shortcut baseline).
    linus_head: bytes | None = None
    linus_cfg = settings.trees.get("linus")
    if linus_cfg is not None:
        linus_disk_path = linus_cfg.path
        if not linus_disk_path.is_absolute():
            from mimir.config import PROJECT_ROOT

            linus_disk_path = PROJECT_ROOT / linus_disk_path
        if linus_disk_path.exists():
            linus_head = _read_linus_head(linus_disk_path)

    # Linus first so its clone exists for --reference on subsequent
    # trees in the same tick.
    ordered_slugs = ["linus"] + [s for s in settings.trees if s != "linus"]
    for slug in ordered_slugs:
        tree = settings.trees.get(slug)
        if tree is None:
            continue
        tr = TreeWalkResult()
        result.trees[slug] = tr

        # Cadence gate: skip trees whose last walk is still within
        # the configured interval. This prevents re-walking
        # force-pushed trees (linux-next) or slow trees (linus)
        # on every scheduler tick regardless of their cadence.
        with SessionLocal() as session:
            state = session.get(MainlineState, slug)
            last = state.last_walked_at if state else None
        if (
            last is not None
            and (now - aware_utc(last)).total_seconds() < tree.walk_every_seconds
        ):
            tr.skipped = True
            continue

        try:
            tree_path = _ensure_tree(
                tree,
                reference=linus_path if slug != "linus" else None,
                skip_fetch=skip_fetch,
            )
            if slug == "linus":
                linus_path = tree_path
                if not skip_maintainers:
                    ran, loaded, head_sha = load_maintainers(
                        tree_path,
                        tree_name=slug,
                        force=force,
                    )
                    result.maintainers_ran = ran
                    result.maintainers_unchanged = not ran
                    result.subsystems_loaded = loaded
                    result.mainline_head = head_sha

            if not skip_commits:
                # Phase 3 of the two-pool restructure: reads from
                # the active ReadSessionPool (query_only=1 session);
                # writes dispatch through the active WriterThread via
                # _submit_mainline_batch + _submit_mainline_cursor_update.
                # No write_transaction needed here; the writer thread
                # handles BEGIN IMMEDIATE internally per WriteOp.
                from mimir.broker import _context

                read_pool = _context.get_active_pool()
                active_writer = _context.get_active_writer()

                with read_pool.session() as session:
                    walk = walk_commits(
                        session,
                        tree_path,
                        tree_name=slug,
                        writer=active_writer,
                        batch_size=settings.mainline_commit_batch_size,
                        branch=tree.branch,
                        rebases=tree.rebases,
                        exclude_from=linus_head if slug != "linus" else None,
                    )
                tr.commits_seen = walk.commits_seen
                tr.commits_linked = walk.linked
                tr.rows_inserted = walk.rows_inserted
                result.commits_seen += walk.commits_seen
                result.commits_linked += walk.linked
                result.rows_inserted += walk.rows_inserted
                result.commits_ran = True

            # Advance the cadence cursor on any successful tick,
            # regardless of which phases ran. Only failed walks (those
            # that raised above) leave the cursor untouched so the next
            # tick retries.
            #
            # Phase 6a dual-dispatch fork: when a broker writer is
            # active (steady-state serving, the scheduler's
            # `update-mainline` tick on the long worker), route the
            # cadence write through the single WriterThread so it
            # joins the single-writer invariant. The UPSERT touches
            # ONLY `last_walked_at`, so it never clobbers
            # `last_commit_sha` / `commits_walked_to_sha` written by
            # the maintainers-replace / cursor WriteOps. Fall back to
            # the legacy SessionLocal get-or-create for non-broker
            # callers (the `mimir update-mainline` CLI run pre-serve,
            # tests, dev scripts).
            from mimir.broker import _context

            try:
                writer = _context.get_active_writer()
            except RuntimeError:
                writer = None

            if writer is not None:
                _submit_last_walked_at(writer, slug, now).result(timeout=60)
            else:
                with SessionLocal() as session:
                    state = session.get(MainlineState, slug)
                    if state is None:
                        state = MainlineState(tree_name=slug)
                        session.add(state)
                    state.last_walked_at = now
                    session.commit()
        except Exception as exc:
            logger.warning("update_mainline: tree %s failed: %r", slug, exc)
            tr.ok = False
            tr.error = repr(exc)
            continue

    return result


__all__ = [
    "WalkResult",
    "TreeWalkResult",
    "UpdateMainlineResult",
    "extract_message_ids",
    "walk_commits",
    "load_maintainers",
    "update_mainline",
]
