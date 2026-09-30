"""Single-epoch recovery, executed by the broker's long worker."""

from pathlib import Path

from sqlalchemy import delete, select, update

from mimir.broker._context import get_active_pool, get_active_writer
from mimir.broker.writes import WriteOp
from mimir.ingest.epoch import DEFAULT_WORKERS, ingest_epoch
from mimir.models import ArticleList, Inbox, IngestState
from mimir.thread_roots import drive_passes


def reindex_epoch(
    inbox_name: str,
    epoch: str,
    *,
    from_scratch: bool = False,
    workers: int = DEFAULT_WORKERS,
) -> dict:
    """Reset the cursor, re-walk, and repair roots even if the walk fails."""
    writer = get_active_writer()
    with get_active_pool().session() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == inbox_name))
        if inbox is None:
            raise ValueError(f"unknown inbox: {inbox_name!r}")
        inbox_id = inbox.id
        epoch_path = Path(inbox.mirror_path) / epoch
        if not epoch_path.exists():
            raise FileNotFoundError(f"epoch repo not found: {epoch_path}")

        def reset(conn):
            deleted = 0
            if from_scratch:
                # Keep Articles: other inboxes may link the same cross-posts.
                deleted = conn.execute(
                    delete(ArticleList).where(
                        ArticleList.inbox_id == inbox_id,
                        ArticleList.epoch == epoch,
                    )
                ).rowcount
                if deleted:
                    # Survivors in other epochs can lose a parent hop. NULL
                    # roots make readers use the recursive walk until repaired.
                    conn.execute(
                        update(ArticleList)
                        .where(ArticleList.inbox_id == inbox_id)
                        .values(thread_root_id=None)
                    )
            conn.execute(
                update(IngestState)
                .where(
                    IngestState.inbox_id == inbox_id,
                    IngestState.epoch == epoch,
                )
                .values(last_commit_sha=None)
            )
            return deleted

        # A timeout would abandon a reset that can still commit later,
        # leaving no handler to re-walk or repair its cleared roots.
        deleted = writer.submit(
            WriteOp(label=f"reindex:reset:{inbox_name}:{epoch}", fn=reset)
        ).result()
        session.rollback()
        counts = None
        try:
            result = ingest_epoch(session, inbox, epoch, epoch_path, workers=workers)
        finally:
            if from_scratch and deleted:
                # A failed re-walk must not leave the entire inbox unrooted.
                session.rollback()

                def run_pass(fn):
                    return (
                        writer.submit(
                            WriteOp(
                                label=f"thread_roots:{fn.__name__}:{inbox_name}",
                                fn=lambda conn: fn(conn, inbox_id),
                            )
                        ).result(timeout=600)
                        or 0
                    )

                counts = drive_passes(run_pass)
                if counts["exhausted"]:
                    raise ValueError(
                        f"thread-root rebuild for {inbox_name} hit the pass "
                        "budget; rows remain unrooted, re-run "
                        "`mimir backfill-thread-roots`"
                    )
    return {
        "ingest": result.model_dump(mode="json"),
        "deleted": deleted,
        "roots": counts,
    }
