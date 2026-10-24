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
    # ANALYZE's per-index row sample, applied to every connection so
    # `mimir analyze`, auto-ANALYZE-after-ingest and any ad-hoc session
    # share it. The default is 0, no limit: a full pass costs 16.7 s of
    # writer lock on the production corpus (2026-10-22). A positive
    # limit reads the first N rows of each key and records N + 1 for
    # any bigger index, which misled the planner (#817); only raise it
    # knowingly, and re-validate EXPLAIN on the production corpus.
    # See `Settings.analyze_limit` and CONTEXT.md "ANALYZE sample size".
    cur.execute(f"PRAGMA analysis_limit={settings.analyze_limit}")
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
