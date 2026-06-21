"""Writer-side infrastructure for the broker two-pool restructure
(`_claude/specs/2026-05-29-broker-two-pool-design.md`).

Three primitives:
- `WriteOp`: dataclass holding a label + callable that runs inside
  one BEGIN IMMEDIATE transaction on the writer thread's connection.
- `WriteFuture`: alias for `concurrent.futures.Future[None]`,
  returned by `WriterThread.submit()`. Set when the commit
  completes (or with the exception on rollback).
- `WriterThread`: the actor itself. Single thread, one writable
  SQLAlchemy connection, bounded queue, BEGIN IMMEDIATE per op.

Every broker write funnels through the single `WriterThread` as of
the Phase 6 completion (see CONTEXT.md), except VACUUM and the
pre-serve startup bootstrap."""

from __future__ import annotations

import dataclasses
import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Callable

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection

from mimir.config import settings
from mimir.extensions import _sqlite_pragmas

logger = logging.getLogger(__name__)
WriteFuture = Future  # type alias; parametrised as Future[T] at the use site (T is the WriteOp closure's return type; None for closures that don't return anything)

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

    def __init__(
        self,
        database_url: str,
        queue_depth: int,
        slow_warn_ms: int = 2000,
    ) -> None:
        self._database_url = database_url
        self._queue: queue.Queue = queue.Queue(maxsize=queue_depth)
        self._slow_warn_ms = slow_warn_ms
        self._thread: threading.Thread | None = None
        # `_stop_lock` is held by both `submit()` (check `_stopped` +
        # `_queue.put`) and `stop()` (set `_stopped` + put sentinel) so
        # the two operations are atomic with respect to each other.
        # Without this, a `submit()` whose `_stopped` check passes could
        # `put()` AFTER `stop()` enqueued the shutdown sentinel; the
        # writer would process the sentinel and exit, leaving the
        # submitted future unresolved and the caller hung on `.result()`.
        # Holding the lock briefly across `_queue.put()` is fine: the
        # writer thread drains continuously, so backpressure stalls are
        # bounded by the writer's per-op commit time, not by the lock.
        # `_stopped` is a `threading.Event` rather than a bool so that
        # the set/is_set primitives are documented thread-safe (the
        # internal `_cond` provides the memory barrier).
        self._stop_lock = threading.Lock()
        self._stopped = threading.Event()

    @classmethod
    def from_settings(cls) -> "WriterThread":
        return cls(
            database_url=settings.database_url,
            queue_depth=settings.broker_writer_queue_depth,
            slow_warn_ms=settings.broker_slow_rpc_warn_ms,
        )

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
        future: WriteFuture = Future()
        with self._stop_lock:
            if self._stopped.is_set():
                raise RuntimeError("WriterThread is stopped")
            self._queue.put((op, future))
        return future

    def stop(self, timeout: float = 30.0) -> None:
        """Signal the writer to drain its queue and exit. Idempotent."""
        with self._stop_lock:
            if self._stopped.is_set():
                return
            self._stopped.set()
            self._queue.put(_SHUTDOWN_SENTINEL)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        engine = create_engine(self._database_url, future=True)
        # Attach the same per-connection PRAGMA registration the shared
        # engine in mimir.extensions uses. Without this, the writer's
        # connection lacks foreign_keys=ON (silently breaks
        # ondelete=CASCADE), synchronous=NORMAL (every commit fsyncs
        # the WAL twice), analysis_limit=N (any ANALYZE on the writer
        # holds the lock for the full multi-second scan), and
        # busy_timeout. The listener correctly skips query_only=1 when
        # mimir_is_broker=true, so attaching it here doesn't make the
        # writer read-only.
        event.listen(engine, "connect", _sqlite_pragmas)
        try:
            # `with engine.connect()` so a `connect()` raise (wrong
            # database_url, missing /data/db mount, permission error,
            # SQLite busy at open) propagates out cleanly instead of
            # leaving an unbound `conn` that the old bare-assignment
            # finally couldn't `.close()`. The previous shape died
            # silently on UnboundLocalError, the writer thread exited,
            # and every subsequent `submit()` blocked forever on the
            # full queue with the broker still advertising as healthy.
            with engine.connect() as conn:
                while True:
                    item = self._queue.get()
                    if item is _SHUTDOWN_SENTINEL:
                        return
                    op, future = item
                    self._run_one(conn, op, future)
        finally:
            engine.dispose()

    def _run_one(self, conn, op: WriteOp, future: WriteFuture) -> None:
        t0 = time.perf_counter()
        try:
            with conn.begin():
                # SQLAlchemy 2.0 conn.begin() is the standard transaction
                # context. This thread is the sole SQLite writer in the
                # broker (two-pool Phase 6 complete), so the default
                # DEFERRED begin is safe: there is no concurrent writer to
                # race a snapshot upgrade against, and the writer's fresh
                # engine wouldn't fire any shared-engine begin listener
                # anyway.
                result = op.fn(conn)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            if elapsed_ms >= self._slow_warn_ms:
                logger.warning(
                    "broker slow write [%s] (%dms)",
                    op.label,
                    elapsed_ms,
                )
            future.set_result(result)
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)
