"""Run state for the chunked backfills, so an interrupted run resumes
where it stopped instead of at the newest article (#680).

A run is one walk of one backfill with one parameter set. Each broker
chunk resolves its run by `(name, params_key)` and starts from the
run's cursor, so the broker owns the cursor and a killed CLI or a
broker restart loses at most the batch in flight. Re-running the same
command is the recovery.

- The cursor is written by `advance`, run as the FINAL statement of
  each batch's WriteOp, so it commits with the rows it covers (the
  `IngestState` pattern, see CONTEXT.md Phase 3b). The writes are
  idempotent, so redoing the one uncommitted batch is harmless.
- `finish` marks a run finished once its walk finds no more rows. A
  finished run is never resumed; the same command afterwards starts a
  new run from the newest article. A run stopped by `--limit` stays
  unfinished, so the next session continues it.
- `restart=True` deletes the unfinished run before starting a new one.
  Deleted, not marked finished, so a finished row always means a walk
  that reached the oldest article.

Concurrency: two CLIs on the same key join the same run. Their chunks
serialise on the broker's single long worker by default; with more long
workers a batch may be processed twice, which the idempotent writes
absorb, and `advance` only ever lowers the cursor. No locking.

Every write goes through the broker's WriterThread.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, insert, select, update

from mimir.models import BackfillRun


@dataclass(frozen=True)
class Run:
    """A run as one chunk found it: `cursor` and `examined` are the
    values before the chunk's first batch."""

    id: int
    started_at: datetime
    cursor: int | None
    examined: int


def params_key(*, reprocess: bool, inbox_filter: str | None = None) -> str:
    """The parameters that define which articles a run walks, as a
    canonical string. The ONE builder of the key, for both the lookup
    and the insert in `resolve`.

    `limit` is deliberately absent: it caps a session, not a run, so a
    run started with `--limit` continues without one."""
    return json.dumps(
        {"inbox_filter": inbox_filter, "reprocess": reprocess}, sort_keys=True
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def resolve(name: str, key: str, *, restart: bool = False) -> Run:
    """Return the unfinished run for `(name, key)`, starting one if
    there is none. `restart` discards the unfinished run first.

    One WriteOp, so the lookup and the insert cannot race another
    chunk resolving the same key."""
    from mimir.broker._context import get_active_writer
    from mimir.broker.writes import WriteOp

    unfinished = (
        (BackfillRun.name == name)
        & (BackfillRun.params_key == key)
        & BackfillRun.finished_at.is_(None)
    )
    columns = (
        BackfillRun.id,
        BackfillRun.started_at,
        BackfillRun.cursor,
        BackfillRun.examined,
    )

    def _fn(conn) -> Run:
        if restart:
            conn.execute(delete(BackfillRun).where(unfinished))
        row = conn.execute(select(*columns).where(unfinished)).one_or_none()
        if row is None:
            now = _now()
            conn.execute(
                insert(BackfillRun).values(
                    name=name,
                    params_key=key,
                    examined=0,
                    started_at=now,
                    updated_at=now,
                )
            )
            row = conn.execute(select(*columns).where(unfinished)).one()
        return Run(*row)

    op = WriteOp(label=f"backfill_runs:{name}:resolve", fn=_fn)
    return get_active_writer().submit(op).result(timeout=60)


def advance(run_id: int, cursor: int, examined: int):
    """A statement for the end of a batch's WriteOp: move the run's
    cursor down to `cursor` and add the batch's `examined` count.

    `MIN` keeps the cursor from moving back up when two chunks of one
    run overlap; SQLite's two-argument `min` returns NULL when either
    side is NULL, hence the `COALESCE` for a run's first batch."""

    def _after(conn) -> None:
        conn.execute(
            update(BackfillRun)
            .where(BackfillRun.id == run_id)
            .values(
                cursor=func.coalesce(func.min(BackfillRun.cursor, cursor), cursor),
                examined=BackfillRun.examined + examined,
                updated_at=_now(),
            )
        )

    return _after


def finish(run_id: int) -> None:
    """Mark a run finished: its walk found no more rows."""
    from mimir.broker._context import get_active_writer
    from mimir.broker.writes import WriteOp

    def _fn(conn) -> None:
        now = _now()
        conn.execute(
            update(BackfillRun)
            .where(BackfillRun.id == run_id)
            .values(finished_at=now, updated_at=now)
        )

    op = WriteOp(label="backfill_runs:finish", fn=_fn)
    get_active_writer().submit(op).result(timeout=60)
