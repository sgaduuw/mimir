import contextvars
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from mimir.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _conn_record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
    # Maintenance toggle: when this container is flagged read-only,
    # block writes at the SQLite layer so anything that slipped past
    # the cache.set short-circuit raises instead of silently competing
    # for the writer lock. See Settings.read_only_db.
    if settings.read_only_db:
        cur.execute("PRAGMA query_only=1")
    cur.close()


# Per-block opt-in flag. Toggled by `write_transaction()` so the
# `begin` event listener below knows whether the next BEGIN should
# be IMMEDIATE (writer lock acquired upfront) or DEFERRED (SQLAlchemy
# default; the connection only takes the writer lock on its first
# write statement).
_WRITE_TXN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "mimir_write_txn", default=False,
)


@event.listens_for(engine, "begin")
def _begin_immediate_when_marked(conn) -> None:
    """For transactions marked write-heavy via `write_transaction()`,
    issue `BEGIN IMMEDIATE` instead of the default DEFERRED begin,
    AND raise `busy_timeout` on the connection for the duration of
    the block.

    The motivation for IMMEDIATE is `SQLITE_BUSY_SNAPSHOT`:
    SQLAlchemy's deferred begin lets the first statement of a
    transaction be a read, which takes a snapshot of the DB; the
    transaction is then promoted to a writer on the first write
    statement. If another connection committed a write between the
    snapshot and the promote attempt, SQLite returns
    `SQLITE_BUSY_SNAPSHOT` immediately (the snapshot is now
    inconsistent with the live DB). That error surfaces as
    `OperationalError("database is locked")` and is **not**
    retryable via `busy_timeout`, the only recovery is to roll back
    and restart the transaction. BEGIN IMMEDIATE acquires the
    writer lock at transaction start, so the read-then-write
    upgrade can't happen.

    The motivation for raising `busy_timeout` is that `BEGIN
    IMMEDIATE` itself can fail with the (recoverable)
    `SQLITE_BUSY` when another connection is writing at that
    moment. The web-tier default (5s) is right for request
    handlers but too short for a one-shot backfill under load:
    after an `archive_stats` cache invalidation, every cold-miss
    render writes its computed value, and a few hundred renders in
    a 5s window starve a concurrent backfill. CLI workloads have
    no latency budget; bump to the (default 60s) writes timeout.
    Reset on pool checkin so the bump doesn't leak to subsequent
    non-write callers.
    """
    if _WRITE_TXN.get():
        conn.exec_driver_sql(
            f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms_writes}"
        )
        conn.exec_driver_sql("BEGIN IMMEDIATE")


@event.listens_for(engine, "reset")
def _restore_default_busy_timeout(dbapi_connection, _record, _state) -> None:
    """Reset `busy_timeout` to the web-tier default when a connection
    is returned to the pool. Paired with the bump in
    `_begin_immediate_when_marked`: a write_transaction()-promoted
    connection going back to the pool would otherwise hand the
    higher value to whoever checks it out next (typically a web
    request), violating the short-timeout contract there. Cheap
    (one PRAGMA per checkin)."""
    cur = dbapi_connection.cursor()
    cur.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
    cur.close()


@contextmanager
def write_transaction():
    """Mark every transaction started inside this block as write-heavy
    so its `BEGIN` is `IMMEDIATE` (see `_begin_immediate_when_marked`).

    Apply at the top of long-running CLI workloads where the session
    interleaves SELECTs and INSERT/UPDATEs: `ingest_epoch`,
    `walk_articles` (the shared backfill walker), `backfill_canonicals`,
    `update-mainline`'s commit walker. Without the wrapper, the
    first write after preceding reads can fail with
    `SQLITE_BUSY_SNAPSHOT` whenever a concurrent writer (web-tier
    cache.set, another CLI workload) committed in between.

    Read-only paths and the web tier do NOT need this; they stay
    deferred so the writer lock is held only by code that actually
    writes.
    """
    token = _WRITE_TXN.set(True)
    try:
        yield
    finally:
        _WRITE_TXN.reset(token)
