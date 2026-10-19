"""Re-parse persisted `parse_failures` rows after a parser fix.

`ingest_epoch` records every parse failure as a row in `parse_failures`;
this module re-walks them sequentially. On success: insert the Article
(or cross-post link) and delete the row. On failure: bump
attempts/last_attempt and refresh the error fields. Used by both the
`admin failures replay` CLI command and any operator-driven cleanup
after a parser change.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

from dulwich.errors import NotGitRepository
from dulwich.repo import Repo
from pydantic import BaseModel
from sqlalchemy import delete, insert, select, update

from mimir.ingest.epoch import _to_article_insert
from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    ArticleTrailer,
    Inbox,
    ParseFailure,
)
from mimir.parser import parse_message

logger = logging.getLogger(__name__)


class ReplayResult(BaseModel):
    """Outcome of replaying persisted parse failures for one inbox."""

    attempted: int = 0
    recovered: int = 0  # parsed cleanly; row deleted, article inserted/linked.
    still_failed: int = 0  # parse still raises; row's last_attempt + attempts updated.
    skipped: int = 0  # blob couldn't be fetched (mirror missing, ref pruned).


def _replay_loop(
    conn,
    inbox_id: int,
    inbox_mirror_path: str,
    epoch_filter: str | None,
    limit: int | None,
) -> ReplayResult:
    """Core replay logic, run on the writer thread's Core Connection.

    Writes use Core DML statements with RETURNING for new Article ids.
    The dulwich blob fetches read from the mirror on disk, no connection
    involvement.
    """
    out = ReplayResult()

    q = select(ParseFailure).where(ParseFailure.inbox_id == inbox_id)
    if epoch_filter is not None:
        q = q.where(ParseFailure.epoch == epoch_filter)
    q = q.order_by(ParseFailure.epoch, ParseFailure.commit_sha)
    if limit is not None:
        q = q.limit(limit)

    rows = list(conn.execute(q).mappings())

    # still-failed updates accumulate and are written after the loop.
    still_failed_updates: list[dict] = []
    # Message ids recovered by this run, for the thread-root passes.
    recovered_msgids: list[str] = []
    for row in rows:
        row_epoch = row["epoch"]
        row_commit_sha = row["commit_sha"]
        out.attempted += 1
        repo_path = Path(inbox_mirror_path) / row_epoch
        # Open the Repo with a context manager and close it as soon as the
        # row's blob + commit_time are extracted. Don't accumulate Repo
        # instances across the function lifetime: dulwich's pack-reading C
        # state assumes single-threaded access to a given .git, and two
        # concurrent Repos over the same path (replay vs warm handler, two
        # concurrent admin replays) can race on the shared pack index cache
        # on free-threaded 3.14t and later (audit #480).
        try:
            with Repo(str(repo_path)) as repo:
                try:
                    commit = repo[row_commit_sha.encode()]
                    tree = repo[commit.tree]
                    _mode, blob_sha = tree[b"m"]
                    raw = repo[blob_sha].data
                    commit_time = datetime.fromtimestamp(commit.commit_time, UTC)
                except KeyError:
                    # Commit or `m` blob missing; mirror was pruned or rewound.
                    # Leave the row in place for the operator to inspect.
                    out.skipped += 1
                    continue
        except NotGitRepository, FileNotFoundError:
            out.skipped += 1
            continue

        try:
            parsed = parse_message(raw)
        except Exception as exc:
            # Defer to a batch update after the loop.
            still_failed_updates.append(
                {
                    "_epoch": row_epoch,
                    "_commit_sha": row_commit_sha,
                    "_error_class": type(exc).__name__,
                    "_error_message": str(exc)[:1000],
                    "_now": datetime.now(UTC),
                }
            )
            out.still_failed += 1
            continue

        existing_id = conn.execute(
            select(Article.id).where(Article.message_id == parsed.message_id)
        ).scalar_one_or_none()
        if existing_id is None:
            # session=None: replay is single-row on a raw connection, so no
            # in-series parent lookup; the backfill command closes any gap
            # in patch_series_key + patch_series_version later.
            rec = _to_article_insert(
                parsed,
                inbox_id,
                row_epoch,
                row_commit_sha,
                commit_time,
                canonical_inbox_id=None,
                session=None,
            )
            result = conn.execute(
                insert(Article)
                .values(
                    message_id=rec.message_id,
                    subject=rec.subject,
                    author=rec.author,
                    date=rec.date,
                    thread_parent=rec.thread_parent,
                    subject_normalized=rec.subject_normalized,
                    canonical_inbox_id=rec.canonical_inbox_id,
                    patch_series_key=rec.patch_series_key,
                    patch_series_version=rec.patch_series_version,
                    patch_series_position=rec.patch_series_position,
                )
                .returning(Article.id)
            )
            new_article_id = result.scalar_one()
            conn.execute(
                insert(ArticleList).values(
                    article_id=new_article_id,
                    inbox_id=inbox_id,
                    epoch=row_epoch,
                    commit_sha=row_commit_sha,
                )
            )
            if rec.touched_paths:
                conn.execute(
                    insert(ArticleFile).values(
                        [
                            {"article_id": new_article_id, "path": p}
                            for p in rec.touched_paths
                        ]
                    )
                )
            if rec.trailer_rows:
                conn.execute(
                    insert(ArticleTrailer).values(
                        [
                            {
                                "article_id": new_article_id,
                                "role": role,
                                "name": name,
                                "address": address,
                                "address_normalized": address.lower(),
                            }
                            for role, name, address in rec.trailer_rows
                        ]
                    )
                )
        else:
            already_linked = conn.execute(
                select(ArticleList.article_id).where(
                    ArticleList.article_id == existing_id,
                    ArticleList.inbox_id == inbox_id,
                )
            ).scalar_one_or_none()
            if already_linked is None:
                conn.execute(
                    insert(ArticleList).values(
                        article_id=existing_id,
                        inbox_id=inbox_id,
                        epoch=row_epoch,
                        commit_sha=row_commit_sha,
                    )
                )
        conn.execute(
            delete(ParseFailure).where(
                ParseFailure.inbox_id == inbox_id,
                ParseFailure.epoch == row_epoch,
                ParseFailure.commit_sha == row_commit_sha,
            )
        )
        out.recovered += 1
        recovered_msgids.append(parsed.message_id)

    # Replay inserts `article_lists` rows directly, so their
    # materialised thread root is unset. Left alone those rows stay
    # NULL permanently, even on a corpus where the backfill has already
    # run and the operator has no reason to run it again, and every
    # later reply to one of them stays NULL too. Resolving here keeps
    # the column self-maintaining rather than leaving a hole that only
    # a remembered follow-up command would close.
    #
    # Idempotent and cheap: the passes only touch rows that are still
    # NULL, so on a fully-backfilled corpus with nothing recovered this
    # is a couple of no-op statements.
    if out.recovered:
        _resolve_roots_after_replay(conn, inbox_id, recovered_msgids)

    # Flush still-failed updates: one UPDATE per row so each carries
    # its specific error_class / error_message.
    for upd in still_failed_updates:
        conn.execute(
            update(ParseFailure)
            .where(
                ParseFailure.inbox_id == inbox_id,
                ParseFailure.epoch == upd["_epoch"],
                ParseFailure.commit_sha == upd["_commit_sha"],
            )
            .values(
                last_attempt=upd["_now"],
                attempts=ParseFailure.attempts + 1,
                error_class=upd["_error_class"],
                error_message=upd["_error_message"],
            )
        )

    return out


def _resolve_roots_after_replay(
    conn_or_session, inbox_id: int, recovered_msgids: list[str]
) -> None:
    """Run the thread-root passes for one inbox after a replay.

    `replay_failures` holds the writer's Connection. The passes only
    `execute(text(...))` and read `.rowcount`, which a Session supports
    identically, so neither needs wrapping.

    Two halves, and the passes are only the second one.

    **Invalidate what the recovered article now owns.** A reply that
    arrived while its parent sat in `parse_failures` found no parent in
    this inbox and correctly self-rooted, which makes its row NON-NULL.
    All three passes gate on `thread_root_id IS NULL`, so they skip that
    row forever: the recovered parent takes one root and its waiting
    subtree keeps another, permanently, with both halves rendering fine
    and nothing erroring. No shipped command repairs it either, because
    every repair path keys on NULL too.

    So each recovered article's descendants are set back to NULL first,
    which is the state that means "not yet computed" (readers fall back
    to the recursive CTE) and the only state the passes below can act
    on. This mirrors what ingest does for the same arrival order via
    `_resolve_thread_root`; replay previously reused only the
    convergence half.

    **Then converge.** The same idempotent passes the backfill uses,
    which now see the invalidated rows and recompute the whole subtree
    onto the recovered parent's root.
    """
    from mimir.ingest._pending import _set_subtree_root
    from mimir.thread_roots import drive_passes

    for msgid in recovered_msgids:
        article_id = conn_or_session.execute(
            select(Article.id).where(Article.message_id == msgid)
        ).scalar_one_or_none()
        if article_id is None:
            # Recovered as a cross-post link to an article that has
            # since gone (an inbox delete racing a replay). Nothing to
            # re-root.
            continue
        _set_subtree_root(conn_or_session, inbox_id, article_id, None)

    counts = drive_passes(lambda fn: fn(conn_or_session, inbox_id))
    if counts["exhausted"]:
        logger.warning(
            "thread-roots: inbox %s hit the pass budget during replay; "
            "rows remain unrooted, run backfill-thread-roots",
            inbox_id,
        )


def replay_failures(
    inbox: Inbox,
    epoch_filter: str | None = None,
    limit: int | None = None,
) -> ReplayResult:
    """Re-parse persisted parse_failures rows for `inbox`.

    On success: insert the Article (or cross-post link) and delete the
    failure row. On failure: bump attempts/last_attempt and refresh the
    error fields. Sequential by design, replay is a low-volume admin
    op, not the hot ingest path.

    The full replay loop runs inside a single WriteOp closure dispatched
    to the broker's writer thread. Raises RuntimeError when no writer is
    active.
    """
    from mimir.broker.writes import submit_write

    # Capture inbox attributes now; `inbox` may be a detached ORM
    # object and reading its attributes later (in a different thread)
    # is safe because we're only reading scalars that were already
    # loaded, not triggering lazy loads.
    inbox_id = inbox.id
    inbox_mirror_path = inbox.mirror_path

    def _fn(conn):
        return _replay_loop(
            conn,
            inbox_id=inbox_id,
            inbox_mirror_path=inbox_mirror_path,
            epoch_filter=epoch_filter,
            limit=limit,
        )

    return submit_write("failures_replay", _fn, timeout=300)
