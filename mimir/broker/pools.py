"""Read-side session pool for the broker two-pool restructure
(`_claude/specs/2026-05-29-broker-two-pool-design.md`).

Wraps a SQLAlchemy session factory whose pool enforces
`PRAGMA query_only = 1` on every connect. The broker's read pool
checks sessions out of this for the "compute / SQL-read" phase of
every RPC; multiple read-pool threads hold sessions in parallel
with zero writer-lock contention. The handler migration onto this
pool completed across Phases 2-6 (see CONTEXT.md)."""

from __future__ import annotations

import contextlib
import logging
import threading

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from mimir.config import settings

logger = logging.getLogger(__name__)


class ReadSessionPool:
    """Holds one SQLAlchemy engine + sessionmaker configured to issue
    `PRAGMA query_only = 1` on every connect. Sessions are checked
    out via the `session()` context manager.

    Threading model: the engine's underlying connection pool is
    shared across threads; SQLAlchemy is documented thread-safe for
    engine + sessionmaker. Sessions themselves are NOT thread-safe,
    which is fine because each `with pool.session()` block is scoped
    to one thread."""

    def __init__(self, database_url: str, pool_size: int) -> None:
        self._engine: Engine = create_engine(
            database_url,
            pool_size=pool_size,
            max_overflow=0,
            future=True,
        )

        @event.listens_for(self._engine, "connect")
        def _set_query_only(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute("PRAGMA query_only = 1")
            finally:
                cursor.close()

        self._sessionmaker = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            future=True,
        )
        self._closed = False
        self._closed_lock = threading.Lock()
        # Condition + in-flight counter wire `close()` to wait for
        # checked-out sessions to drain before `engine.dispose()`.
        #
        # The rationale recorded here (audit #472) was that without the
        # drain, `dispose()` invalidates connections mid-query and a
        # warm handler hits OperationalError on its next statement.
        # Measured against the pinned SQLAlchemy 2.0.51 with QueuePool,
        # that does not happen: `dispose()` closes only IDLE
        # connections, while a checked-out one is detached and keeps
        # working. What the drain still buys is deterministic close at
        # shutdown. Re-measure before deleting it (the pinned-runtime
        # rule); note also that
        # `test_read_session_pool_close_drain_timeout_proceeds_anyway`
        # asserts `not errors` while its own docstring says the stuck
        # handler errors.
        self._drained = threading.Condition(self._closed_lock)
        self._in_flight = 0

    @classmethod
    def from_settings(cls) -> "ReadSessionPool":
        return cls(
            database_url=settings.database_url,
            pool_size=settings.broker_read_pool_size,
        )

    @contextlib.contextmanager
    def session(self):
        """Yield a query_only=1 session; close on exit. Raises
        RuntimeError if the pool has been closed.

        The session is constructed while holding `_closed_lock` so a
        concurrent `close()` cannot dispose the engine between the
        closed-check and the sessionmaker call. The lock is released
        before yielding, so session use itself does not block other
        checkouts. An in-flight counter is bumped under the lock so
        `close()` can wait for live sessions to drain before
        `engine.dispose()`."""
        with self._closed_lock:
            if self._closed:
                raise RuntimeError("ReadSessionPool is closed")
            s: Session = self._sessionmaker()
            self._in_flight += 1
        try:
            yield s
        finally:
            try:
                s.close()
            finally:
                with self._closed_lock:
                    self._in_flight -= 1
                    if self._in_flight == 0:
                        self._drained.notify_all()

    def close(self, drain_timeout: float = 30.0) -> None:
        """Mark the pool closed, wait for in-flight sessions to drain,
        then dispose the engine. Idempotent.

        `drain_timeout` (default 30s) bounds the wait. `dispose()`
        runs even after a timeout so a stuck handler can never block
        broker shutdown. (This used to add that on timeout the
        in-flight sessions hit OperationalError on their next
        statement; on SQLAlchemy 2.0.51 they do not, see `__init__`.)
        In practice the broker's shutdown sequence sets
        `stop_event` before calling `close()`, so workers have
        already returned by the time the drain wait starts."""
        with self._closed_lock:
            if self._closed:
                return
            self._closed = True
            drained = self._drained.wait_for(
                lambda: self._in_flight == 0,
                timeout=drain_timeout,
            )
            leftover = self._in_flight
        if not drained:
            logger.warning(
                "ReadSessionPool.close: %d sessions still in-flight "
                "after %.1fs; disposing engine anyway",
                leftover,
                drain_timeout,
            )
        self._engine.dispose()
