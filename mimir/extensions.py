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
    # No `analysis_limit`: SQLite's default is 0, every row, which is
    # what ANALYZE must use. See CONTEXT.md "ANALYZE sample size".
    # Single-writer invariant: the broker process IS the SQLite
    # writer; every other process (web, tasks, dev CLI) opens
    # query_only=1 so any errant write path raises instead of
    # silently bypassing the broker's serialised write queue.
    # Anything that slipped past the cache.set broker-dispatch or
    # got added without thinking about broker mode trips
    # `OperationalError: attempt to write a readonly database`
    # here.
    if not settings.mimir_is_broker:
        cur.execute("PRAGMA query_only=1")
    cur.close()
