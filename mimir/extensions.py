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
    issue `BEGIN IMMEDIATE` instead of the default DEFERRED begin.

    The motivation is `SQLITE_BUSY_SNAPSHOT`: SQLAlchemy's deferred
    begin lets the first statement of a transaction be a read,
    which takes a snapshot of the DB; the transaction is then
    promoted to a writer on the first write statement. If another
    connection committed a write between the snapshot and the
    promote attempt, SQLite returns `SQLITE_BUSY_SNAPSHOT` immediately
    (the snapshot is now inconsistent with the live DB). That error
    surfaces as `OperationalError("database is locked")` and is
    **not** retryable via `busy_timeout`, the only recovery is to
    roll back and restart the transaction.

    BEGIN IMMEDIATE acquires the writer lock at transaction start,
    so the read-then-write upgrade can't happen. Other writers
    queue behind us via `busy_timeout` (which IS retryable).

    Used by long-running CLI workloads that interleave reads and
    writes (backfills, ingest, mainline walker) and that previously
    blew up against the gunicorn-side cache writes on a busy
    production deploy. Web reads and best-effort cache writes are
    not marked; they keep using the default deferred begin so they
    don't serialise behind each other unnecessarily.
    """
    if _WRITE_TXN.get():
        conn.exec_driver_sql("BEGIN IMMEDIATE")


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
