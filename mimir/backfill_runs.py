"""Run state for the chunked backfills, so an interrupted run resumes
where it stopped instead of at the newest article (#680).

A run is one walk of one backfill with one parameter set. Each broker
chunk resolves its run by `(name, params_key)` and starts from the
run's cursor, so the broker owns the cursor and a killed CLI or a
broker restart loses at most the batch in flight. Re-running the same
command is the recovery.

- The cursor is written by `advance`, run as the FINAL statement of
  each batch's WriteOp, so it commits with the rows it covers (the
  `IngestState` pattern, see CONTEXT.md Phase 3b). A failed batch rolls
  back whole, cursor included, so redoing it applies it once.
- `finish` marks a run finished once its walk finds no more rows. A
  finished run is never resumed; the same command afterwards starts a
  new run from the newest article. A run stopped by `--limit` stays
  unfinished, so the next session continues it.
- Only a session's first chunk may start a run (`start` set). A later
  chunk joins whatever run is unfinished for the key (another
  session's, if one started meanwhile) and ends the session only when
  there is none, getting the key's latest finished run back.
- `start="restart"` deletes the unfinished run before starting a new
  one, unless the run has examined something: then it is left alone and
  handed back as refused, because a run can be a day into its work and
  the delete takes the evidence with it. `start="force_restart"`
  deletes it regardless. Either way the run found is handed back, so
  the caller can say what was discarded or refused. Deleted, not marked
  finished, so a finished row always means a walk that reached the
  oldest article.

Concurrency: two CLIs on the same key join the same run. Their chunks
serialise on the broker's single long worker by default. With more long
workers, chunks of one run can overlap and process the same articles
twice, and not every backfill's writes are idempotent (canonical
observation counts add up; trailer inserts without `--reprocess` are
plain inserts). `advance` only ever lowers the cursor. No locking.

Every write goes through the broker's WriterThread; `get` reads through
the active read pool.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, func, insert, select, update

from mimir.models import BackfillRun


@dataclass(frozen=True)
class Run:
    """A run as one read found it. For the run `resolve` returns,
    `cursor` and `examined` are the values before the chunk's first
    batch."""

    id: int
    started_at: datetime
    cursor: int | None
    examined: int
    finished: bool


_COLUMNS = (
    BackfillRun.id,
    BackfillRun.started_at,
    BackfillRun.cursor,
    BackfillRun.examined,
    BackfillRun.finished_at.is_not(None),
)


def params_key(*, reprocess: bool, inbox_filter: str | None = None) -> str:
    """The parameters that define which articles a run walks, as a
    canonical string. The ONE builder of the key, for both the lookup
    and the insert in `resolve`.

    `limit` is deliberately absent: it caps a session, not a run, so a
    run started with `--limit` continues without one."""
    return json.dumps(
        {"inbox_filter": inbox_filter, "reprocess": reprocess}, sort_keys=True
    )


@dataclass(frozen=True)
class Resolved:
    """What `resolve` found. `run` is the run to walk, None when there
    is none or `refused` is set. `discarded` is the unfinished run a
    restart deleted; `refused` is the one a plain restart left alone."""

    run: Run | None
    discarded: Run | None = None
    refused: Run | None = None


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def resolve(
    name: str,
    key: str,
    *,
    start: Literal["resume", "restart", "force_restart"] | None,
) -> Resolved:
    """Resolve the unfinished run for `(name, key)`.

    `start` is set on a session's first chunk only: "resume" starts a
    run when there is none, "restart" discards the unfinished run and
    starts a new one unless the run has examined something (then it
    refuses and changes nothing), "force_restart" discards it whatever
    it has done. A later chunk (`start=None`) never starts a run; when
    there is no unfinished run it gets the key's latest run, which
    another session finished, or None.

    One WriteOp, so the lookup, the refuse-or-delete decision and the
    insert cannot race another chunk resolving the same key: a run that
    advanced after the caller last looked is judged as it is now."""
    from mimir.broker._context import get_active_writer
    from mimir.broker.writes import WriteOp

    same_key = (BackfillRun.name == name) & (BackfillRun.params_key == key)
    unfinished = same_key & BackfillRun.finished_at.is_(None)

    def _fn(conn) -> Resolved:
        row = conn.execute(select(*_COLUMNS).where(unfinished)).one_or_none()
        discarded = None
        if row is not None and start in ("restart", "force_restart"):
            old = Run(*row)
            if start == "restart" and old.examined > 0:
                return Resolved(None, refused=old)
            conn.execute(delete(BackfillRun).where(BackfillRun.id == old.id))
            discarded, row = old, None
        if row is None and start is None:
            row = conn.execute(
                select(*_COLUMNS)
                .where(same_key)
                .order_by(BackfillRun.id.desc())
                .limit(1)
            ).one_or_none()
            return Resolved(Run(*row) if row is not None else None)
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
            row = conn.execute(select(*_COLUMNS).where(unfinished)).one()
        return Resolved(Run(*row), discarded)

    op = WriteOp(label=f"backfill_runs:{name}:resolve", fn=_fn)
    return get_active_writer().submit(op).result(timeout=60)


def get(run_id: int) -> Run | None:
    """The run as it is now, or None if `--restart` deleted it."""
    from mimir.broker._context import get_active_pool

    with get_active_pool().session() as session:
        row = session.execute(
            select(*_COLUMNS).where(BackfillRun.id == run_id)
        ).one_or_none()
    return Run(*row) if row is not None else None


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
