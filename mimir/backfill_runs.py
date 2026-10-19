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
- A run is driven by one session at a time. A session's first chunk
  (`start` set, any value) is refused while the run's `updated_at` is
  newer than `LIVE_RUN_SEC`, and handed back as busy; otherwise it
  claims the run in the same WriteOp, so the run looks live before the
  first batch commits. Every committed batch bumps `updated_at`, even
  one that writes no rows. A session that ends on its `--limit`
  releases the run (`release`), so the next invocation continues it
  at once; a killed session cannot, and is waited out.

Concurrency: a second session cannot join a live run, because chunks of
one run that overlap (with more than one long worker) can process the
same articles twice, and not every backfill's writes are idempotent
(canonical observation counts add up; trailer inserts without
`--reprocess` are plain inserts). The guard is a liveness window, not
a lock: a session paused longer than `LIVE_RUN_SEC` between chunks can
be overtaken, and a later chunk (`start=None`) still joins whatever
run is unfinished. `advance` only ever lowers the cursor.

Every write goes through the broker's WriterThread; `get` reads through
the active read pool.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Literal

from sqlalchemy import delete, func, insert, select, update

from mimir.models import BackfillRun

# A run whose `updated_at` is newer than this is being driven, and a
# session's first chunk may not take it. `updated_at` moves on every
# committed batch, and the slowest chunk seen was 65 s waiting on the
# writer (2026-10-16), so 300 s is several times a healthy gap: a
# driven run does not look dead, a killed one is takeable within five
# minutes. Not an option: an override is how two sessions end up on
# one run.
LIVE_RUN_SEC = 300


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
    updated_at: datetime


_COLUMNS = (
    BackfillRun.id,
    BackfillRun.started_at,
    BackfillRun.cursor,
    BackfillRun.examined,
    BackfillRun.finished_at.is_not(None),
    BackfillRun.updated_at,
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
    is none, or when `refused` or `busy` is set. `discarded` is the
    unfinished run a restart deleted; `refused` is the one a plain
    restart left alone; `busy` is one another session is driving, and
    `retry_after` is the seconds until it would be taken."""

    run: Run | None
    discarded: Run | None = None
    refused: Run | None = None
    busy: Run | None = None
    retry_after: int = 0


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
    it has done. All three first refuse, changing nothing, when the run
    is live: another session is driving it (`LIVE_RUN_SEC`). Otherwise
    the session claims the run by bumping its `updated_at`. A later
    chunk (`start=None`) never starts a run; when there is no
    unfinished run it gets the key's latest run, which another session
    finished, or None.

    One WriteOp, so the lookup, the refuse-or-delete decision and the
    insert cannot race another chunk resolving the same key: a run that
    advanced after the caller last looked is judged as it is now."""
    from mimir.broker.writes import submit_write

    same_key = (BackfillRun.name == name) & (BackfillRun.params_key == key)
    unfinished = same_key & BackfillRun.finished_at.is_(None)

    def _fn(conn) -> Resolved:
        row = conn.execute(select(*_COLUMNS).where(unfinished)).one_or_none()
        now = _now()
        if row is not None and start is not None:
            idle = (now - row.updated_at).total_seconds()
            if idle < LIVE_RUN_SEC:
                return Resolved(
                    None, busy=Run(*row), retry_after=ceil(LIVE_RUN_SEC - idle)
                )
        discarded = None
        if row is not None and start in ("restart", "force_restart"):
            old = Run(*row)
            if start == "restart" and old.examined > 0:
                return Resolved(None, refused=old)
            conn.execute(delete(BackfillRun).where(BackfillRun.id == old.id))
            discarded, row = old, None
        if row is not None and start is not None:
            # Claim the run, so it looks live before its first batch
            # commits.
            conn.execute(
                update(BackfillRun)
                .where(BackfillRun.id == row.id)
                .values(updated_at=now)
            )
        if row is None and start is None:
            row = conn.execute(
                select(*_COLUMNS)
                .where(same_key)
                .order_by(BackfillRun.id.desc())
                .limit(1)
            ).one_or_none()
            return Resolved(Run(*row) if row is not None else None)
        if row is None:
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

    return submit_write(f"backfill_runs:{name}:resolve", _fn, timeout=60)


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
    from mimir.broker.writes import submit_write

    def _fn(conn) -> None:
        now = _now()
        conn.execute(
            update(BackfillRun)
            .where(BackfillRun.id == run_id)
            .values(finished_at=now, updated_at=now)
        )

    submit_write("backfill_runs:finish", _fn, timeout=60)


def release(run_id: int) -> None:
    """Let the next session take the run at once: a session that ended
    on its `--limit` is not a driver the next invocation should wait
    for. Ages `updated_at` out of the live window. A killed session
    cannot call this, which is why `LIVE_RUN_SEC` exists."""
    from mimir.broker.writes import submit_write

    def _fn(conn) -> None:
        conn.execute(
            update(BackfillRun)
            .where(BackfillRun.id == run_id, BackfillRun.finished_at.is_(None))
            .values(updated_at=_now() - timedelta(seconds=LIVE_RUN_SEC))
        )

    submit_write("backfill_runs:release", _fn, timeout=60)
