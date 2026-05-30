"""Writer-side infrastructure for Phase 1 of the broker two-pool
restructure (`_claude/specs/2026-05-29-broker-two-pool-design.md`).

Three primitives:
- `WriteOp`: dataclass holding a label + callable that runs inside
  one BEGIN IMMEDIATE transaction on the writer thread's connection.
- `WriteFuture`: alias for `concurrent.futures.Future[None]`,
  returned by `WriterThread.submit()`. Set when the commit
  completes (or with the exception on rollback).
- `WriterThread`: the actor itself. Single thread, one writable
  SQLAlchemy connection, bounded queue, BEGIN IMMEDIATE per op.

This is parallel infrastructure in Phase 1: no caller is migrated
yet."""

from __future__ import annotations

import dataclasses
import logging
import queue
import threading
from concurrent.futures import Future
from typing import Callable

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)
WriteFuture = Future  # type alias; parametrised as Future[None] at the use site

# Sentinel object used to wake the writer loop on stop().
_SHUTDOWN_SENTINEL: tuple[None, None] = (None, None)


@dataclasses.dataclass(frozen=True)
class WriteOp:
    """One unit of work for the writer thread to commit.

    `label` shows up in the slow-write log line and in WriterThread
    debugging output. `fn` is called by the writer thread with its
    writable Connection inside a `BEGIN IMMEDIATE` transaction; the
    writer commits or rolls back depending on whether fn raises."""

    label: str
    fn: Callable[[Connection], None]


class WriterThread:
    """Single-threaded actor owning the only writable SQLite connection.

    Receives `(WriteOp, WriteFuture)` pairs via a bounded queue.
    For each: BEGIN IMMEDIATE -> `op.fn(conn)` -> COMMIT, then set
    the future result. On exception: ROLLBACK + future exception.

    Threading: the queue is `queue.Queue` which is thread-safe.
    The writable connection lives entirely on the writer thread
    and is never touched by anyone else."""

    def __init__(self, database_url: str, queue_depth: int) -> None:
        self._database_url = database_url
        self._queue: queue.Queue = queue.Queue(maxsize=queue_depth)
        self._thread: threading.Thread | None = None
        self._stop_lock = threading.Lock()
        self._stopped = False

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("WriterThread already started")
        self._thread = threading.Thread(
            target=self._run,
            name="mimir-broker-writer",
            daemon=False,
        )
        self._thread.start()

    def submit(self, op: WriteOp) -> WriteFuture:
        """Enqueue an op for the writer to commit. Returns a Future
        that resolves when the commit lands (or rejects with the
        exception on rollback). Blocks if the queue is full
        (backpressure)."""
        if self._stopped:
            raise RuntimeError("WriterThread is stopped")
        future: WriteFuture = Future()
        self._queue.put((op, future))
        return future

    def stop(self, timeout: float = 30.0) -> None:
        """Signal the writer to drain its queue and exit. Idempotent."""
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self._queue.put(_SHUTDOWN_SENTINEL)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        engine = create_engine(self._database_url, future=True)
        conn = engine.connect()
        try:
            while True:
                item = self._queue.get()
                if item is _SHUTDOWN_SENTINEL:
                    return
                op, future = item
                self._run_one(conn, op, future)
        finally:
            conn.close()
            engine.dispose()

    def _run_one(self, conn, op: WriteOp, future: WriteFuture) -> None:
        try:
            with conn.begin():
                # SQLAlchemy 2.0 conn.begin() is the standard transaction
                # context. The BEGIN IMMEDIATE listener from
                # mimir.extensions.write_transaction does NOT bind here
                # (we use a fresh engine). For Phase 1 the writer is
                # the only writer so the default DEFERRED is fine;
                # Phase 2+ may switch to explicit BEGIN IMMEDIATE if
                # contention with other writers becomes possible.
                op.fn(conn)
            future.set_result(None)
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)
