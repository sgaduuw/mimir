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
    # Bound ANALYZE's per-index row sample. SQLite default is 0
    # (no limit), which makes ANALYZE scan every row of every
    # index and hold the writer lock for the full duration; that was
    # ~25 s when measured against an 11M-row corpus, and the corpus is
    # now 28.8M `article_lists` rows (measured 2026-08-04), so an
    # unbounded pass costs proportionally more. Dominant source of
    # broker-side cache.set stalls in production. Setting `analysis_limit` on
    # every connection means `mimir analyze`, auto-ANALYZE-after-
    # ingest, and any ad-hoc session running ANALYZE all inherit
    # the limit uniformly.
    #
    # `Settings.analyze_limit=4000` (the value calibrated for this
    # codebase in 1.36.4; see CONTEXT.md "ANALYZE sample size") is
    # ~10x the SQLite-docs hint for "very large databases". An
    # earlier value of 400 looked appealing from the SQLite docs
    # but produced catastrophic misplans on the 11M-row multi-
    # inbox corpus, recursive-CTE shapes hit ~400-second runtimes
    # against a documented 2 ms baseline. The 4000-row sample
    # gives the planner accurate enough cardinalities for the
    # join shapes the read path leans on; the weekly full
    # ANALYZE (`analyze --full`, `analysis_limit=0`) is the safety
    # net for whatever drifts in the long tail. Don't lower this
    # value without re-validating EXPLAIN on the production
    # corpus, the SQLite docs default is not safe at scale.
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
