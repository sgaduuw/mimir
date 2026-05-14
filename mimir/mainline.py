"""Mainline-tree commit walker.

Walks Linus's `linux.git` (or any other tree pointed at by the
operator) and indexes every `Link: https://lore.kernel.org/.../<msgid>`
trailer it finds. The resulting `mainline_commits` rows let the
patch page render "Applied as `<sha>` on <date>" — the user-visible
signal that closes the lore-archive → mainline-tree loop.

Pure-ish: `extract_message_ids` is a plain function over commit-
message bytes, easy to unit-test. `walk_commits` does the dulwich
walking and writes through to the DB; tested via fake bare repos.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from dulwich.repo import Repo
from pydantic import BaseModel
from sqlalchemy.orm import Session

from mimir.models import MainlineCommit, MainlineState

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
    batch — observed against linux.git commit 9e8e8912b05f."""
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
# resumes — a Ctrl-C mid-walk loses at most this many commits of
# progress but no DB corruption.
_BATCH = 500


def walk_commits(
    session: Session,
    tree_path: Path,
    tree_name: str = "linus",
) -> WalkResult:
    """Walk new commits on `tree_path`, extract `Link:` trailers,
    insert `mainline_commits` rows. Resumable: starts after the
    last walked SHA recorded on `MainlineState`, so steady-state
    ticks only see the commits since the prior run.

    First-run behaviour: walks the entire history. On a full Linus
    tree that's ~1.5M commits and takes minutes. Operator can scope
    the initial run by setting `commits_walked_to_sha` manually
    (e.g. to a recent release tag) if they only care about
    recent history."""
    repo = Repo(str(tree_path))
    state = session.get(MainlineState, tree_name)
    if state is None:
        state = MainlineState(tree_name=tree_name)
        session.add(state)
    head = repo.head()
    since = state.commits_walked_to_sha

    # dulwich's reverse=True walker emits oldest-first, which is
    # what we want: we want to advance the cursor monotonically.
    exclude: list[bytes] = []
    if since:
        try:
            repo[since.encode()]
            exclude = [since.encode()]
        except KeyError:
            # Cursor points at a SHA that no longer exists in the
            # repo (force-push, history rewrite — shouldn't happen
            # on Linus's tree, but be defensive). Re-walk from
            # scratch rather than crash.
            logger.warning(
                "mainline: cursor SHA %s missing from %s; rewalking",
                since, tree_path,
            )
            exclude = []
    walker = repo.get_walker(include=[head], exclude=exclude, reverse=True)

    result = WalkResult(last_walked_sha=since)
    pending_rows: list[MainlineCommit] = []
    last_seen: str | None = since

    def flush() -> None:
        if pending_rows:
            session.add_all(pending_rows)
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
                commit.commit_time, tz=timezone.utc,
            )
            for mid in msgids:
                pending_rows.append(MainlineCommit(
                    commit_sha=sha,
                    message_id=mid,
                    tree_name=tree_name,
                    committed_at=committed_at,
                ))
                result.rows_inserted += 1

        if result.commits_seen % _BATCH == 0:
            flush()
            logger.info(
                "mainline: walked %d commits (%d linked, %d rows)",
                result.commits_seen, result.linked, result.rows_inserted,
            )

    flush()
    return result


__all__ = ["WalkResult", "extract_message_ids", "walk_commits"]
