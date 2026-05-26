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
from datetime import datetime, timezone
from pathlib import Path

from dulwich.repo import Repo
from pydantic import BaseModel
from sqlalchemy import delete, insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from mimir import maintainers
from mimir.config import settings
from mimir.extensions import SessionLocal, write_transaction
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
    branch: str = "HEAD",
    rebases: bool = False,
) -> WalkResult:
    """Walk new commits on `tree_path`, extract `Link:` trailers,
    insert `mainline_commits` rows.

    When `rebases=True`: DELETE the tree's existing rows first then
    walk from `branch` to root with no exclude. Used for force-pushed
    trees like linux-next where the SHA cursor would be invalidated
    daily; INSERT-OR-IGNORE absorbs duplicates intra-walk.

    When `rebases=False` (default): incremental SHA-cursor walk. The
    cursor on `MainlineState.commits_walked_to_sha` is advanced past
    the last seen SHA so the next tick only sees commits since the
    prior run. Missing-cursor defensive rewalk stays in place.

    `branch` selects which ref to walk. Defaults to HEAD; override
    for trees like akpm/mm where the canonical pickup signal lives on
    a non-HEAD branch (e.g. `mm-stable`). Missing branch falls back
    to HEAD with a warning log.

    First-run behaviour (rebases=False): walks the entire history. On
    a full Linus tree that's ~1.5M commits and takes minutes. Operator
    can scope the initial run by setting `commits_walked_to_sha`
    manually (e.g. to a recent release tag) if they only care about
    recent history."""
    repo = Repo(str(tree_path))
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
        # Empty repo or missing ref: nothing to walk. Commit any
        # pending session state (e.g. the new MainlineState row) and
        # return early with zero counts. Note: the rebases=True DELETE
        # has not been issued yet at this point; that block comes after
        # this guard.
        session.commit()
        return WalkResult()

    exclude: list[bytes] = []
    if rebases:
        # Force-pushed tree: wipe the tree's existing rows so stale
        # SHAs don't accumulate. The full walk that follows re-inserts
        # the live history; ON CONFLICT DO NOTHING absorbs any
        # intra-walk duplicates from merge-graph re-emissions.
        session.execute(
            delete(MainlineCommit).where(MainlineCommit.tree_name == tree_name)
        )
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
                exclude = [since.encode()]
            except KeyError:
                logger.warning(
                    "mainline: cursor SHA %s missing from %s; rewalking",
                    since,
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
    last_seen: str | None = result.last_walked_sha

    def flush() -> None:
        if pending_rows:
            # INSERT OR IGNORE on the (commit_sha, message_id) UNIQUE
            # so duplicate observations are silently dropped. Three
            # ways a dup can arrive: (1) two `Link:` URL variants of
            # the same msgid inside one commit message (already
            # deduped at the extract layer in 1.15.1, but defence-
            # in-depth doesn't hurt); (2) dulwich's reverse=True
            # walker re-emitting the same commit from a merge graph
            # within one walk; (3) a cursor-missing rewalk landing
            # on commits already recorded in a prior run. Without
            # ON CONFLICT, any of these aborts the batch and leaves
            # the walker stuck.
            stmt = (
                sqlite_insert(MainlineCommit)
                .values(pending_rows)
                .on_conflict_do_nothing(
                    index_elements=["commit_sha", "message_id"],
                )
            )
            session.execute(stmt)
            pending_rows.clear()
        state.commits_walked_to_sha = last_seen
        session.commit()

    for entry in walker:
        commit = entry.commit
        sha = commit.id.decode("ascii")
        result.commits_seen += 1
        last_seen = sha

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

        if result.commits_seen % _BATCH == 0:
            flush()
            logger.info(
                "mainline: walked %d commits (%d linked, %d rows) on %s",
                result.commits_seen,
                result.linked,
                result.rows_inserted,
                tree_name,
            )

    flush()
    return result


class UpdateMainlineResult(BaseModel):
    """Outcome of one `update_mainline` invocation. Fields are present
    even when their phase was skipped, so callers can branch on
    them without checking which mode was passed; the
    `<phase>_ran` flags carry the dispositive bit."""

    maintainers_ran: bool = False
    maintainers_unchanged: bool = False
    subsystems_loaded: int = 0
    mainline_head: str | None = None

    commits_ran: bool = False
    commits_seen: int = 0
    commits_linked: int = 0
    rows_inserted: int = 0


def _resolve_tree_path() -> Path:
    """Read `Settings.mainline_tree_path` and absolutize against
    PROJECT_ROOT when it's relative. Centralised so both the CLI
    and the broker handler see the same path resolution rules."""
    tree_path = Path(settings.mainline_tree_path)
    if not tree_path.is_absolute():
        # Late import: `PROJECT_ROOT` is a config-module constant
        # but importing it at module load creates a circular
        # dependency with the `Settings` instance.
        from mimir.config import PROJECT_ROOT

        tree_path = PROJECT_ROOT / tree_path
    return tree_path


def _ensure_tree(tree_path: Path, *, skip_fetch: bool) -> None:
    """Clone the mainline tree on first run, fetch otherwise. Skips
    the fetch when `skip_fetch=True` (CLI option that lets an
    operator re-run MAINTAINERS / Link-trailer passes against the
    current HEAD without burning a network round-trip)."""
    if not tree_path.exists():
        tree_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            "mainline: cloning %s -> %s",
            settings.mainline_tree_url,
            tree_path,
        )
        # `--` stops git from interpreting an option-shaped URL as
        # a flag (same defensive pattern as `mimir.sync.clone_epoch`).
        subprocess.run(
            [
                "git",
                "clone",
                "--mirror",
                "--",
                settings.mainline_tree_url,
                str(tree_path),
            ],
            check=True,
        )
        return
    if not skip_fetch:
        logger.debug("mainline: fetching %s", tree_path)
        subprocess.run(
            ["git", "-C", str(tree_path), "fetch", "--quiet", "--prune"],
            check=True,
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
    repo = Repo(str(tree_path))
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

    with (
        write_transaction("update_mainline:maintainers"),
        SessionLocal() as session,
    ):
        state = session.get(MainlineState, tree_name)
        if state is None:
            state = MainlineState(tree_name=tree_name)
            session.add(state)
        if state.last_commit_sha == head_sha and not force:
            return False, 0, head_sha

        parsed = maintainers.parse(blob_bytes)
        # Replace-all in one transaction. The cascade FK on
        # `subsystems.id` clears `subsystem_paths` +
        # `subsystem_maintainers` via ON DELETE CASCADE; SQLite
        # needs `PRAGMA foreign_keys=ON` (set by `mimir.extensions`
        # on every connection) for the cascade to fire.
        session.execute(delete(Subsystem))

        # Three bulk inserts beat ORM's per-row flush. The
        # MAINTAINERS file expands to ~1.5k Subsystem rows + ~10k
        # SubsystemPath rows + ~5k SubsystemMaintainer rows on the
        # kernel tree; under `session.add(row)` the unit-of-work
        # flushed each one individually at commit time, holding the
        # writer lock for the full round-trip count.
        #
        # `sort_by_parameter_order=True` on the RETURNING insert
        # is what aligns the returned ids with the input order so
        # the path / maintainer rows can wire to the right
        # `subsystem_id` without a name lookup. SQLite supports
        # this since 3.35 (RETURNING) and SQLAlchemy 2.0's bulk
        # ORM-style insert honours the option.
        sub_rows = [{"name": sub.name, "status": sub.status} for sub in parsed]
        result = session.execute(
            insert(Subsystem).returning(Subsystem.id),
            sub_rows,
            execution_options={"sort_by_parameter_order": True},
        )
        sub_ids = [row[0] for row in result]

        path_rows = []
        maintainer_rows = []
        for sub_id, sub in zip(sub_ids, parsed, strict=True):
            for glob in sub.files:
                path_rows.append(
                    {
                        "subsystem_id": sub_id,
                        "glob": glob,
                        "is_exclude": False,
                    }
                )
            for glob in sub.excludes:
                path_rows.append(
                    {
                        "subsystem_id": sub_id,
                        "glob": glob,
                        "is_exclude": True,
                    }
                )
            for m in sub.maintainers:
                maintainer_rows.append(
                    {
                        "subsystem_id": sub_id,
                        "role": m.role,
                        "name": m.name,
                        "address": m.address,
                    }
                )
        if path_rows:
            session.execute(insert(SubsystemPath), path_rows)
        if maintainer_rows:
            session.execute(insert(SubsystemMaintainer), maintainer_rows)

        state.last_commit_sha = head_sha
        session.commit()
        loaded = len(parsed)

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


def update_mainline(
    *,
    skip_fetch: bool = False,
    skip_maintainers: bool = False,
    skip_commits: bool = False,
    force: bool = False,
) -> UpdateMainlineResult:
    """Sync the mainline tree and refresh both derived surfaces.

    The two phases (MAINTAINERS reparse + Link-trailer walk) are
    independent; either can be skipped via its flag. `force` only
    affects MAINTAINERS (the commit walker is incremental against
    a cursor, so "force" doesn't have an analogue there).

    Same body as the CLI command had before Phase 2.3; the CLI now
    delegates to this function so the broker handler can call the
    same code path without import gymnastics."""
    tree_name = "linus"  # fixed slug; supports future linux-stable, etc.
    tree_path = _resolve_tree_path()
    _ensure_tree(tree_path, skip_fetch=skip_fetch)

    result = UpdateMainlineResult()

    if not skip_maintainers:
        ran, loaded, head_sha = load_maintainers(
            tree_path,
            tree_name,
            force=force,
        )
        result.maintainers_ran = ran
        result.maintainers_unchanged = not ran
        result.subsystems_loaded = loaded
        result.mainline_head = head_sha

    if not skip_commits:
        with (
            write_transaction("update_mainline:link_trailers"),
            SessionLocal() as session,
        ):
            walk = walk_commits(session, tree_path, tree_name=tree_name)
        result.commits_ran = True
        result.commits_seen = walk.commits_seen
        result.commits_linked = walk.linked
        result.rows_inserted = walk.rows_inserted

    return result


__all__ = [
    "WalkResult",
    "UpdateMainlineResult",
    "extract_message_ids",
    "walk_commits",
    "load_maintainers",
    "update_mainline",
]
