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
from concurrent.futures import Future
from typing import Callable

from sqlalchemy.engine import Connection

WriteFuture = Future  # type alias; parametrised as Future[None] at the use site


@dataclasses.dataclass(frozen=True)
class WriteOp:
    """One unit of work for the writer thread to commit.

    `label` shows up in the slow-write log line and in WriterThread
    debugging output. `fn` is called by the writer thread with its
    writable Connection inside a `BEGIN IMMEDIATE` transaction; the
    writer commits or rolls back depending on whether fn raises."""

    label: str
    fn: Callable[[Connection], None]
