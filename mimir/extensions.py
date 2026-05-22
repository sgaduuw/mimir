import contextvars
import logging
import time
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from mimir.config import settings

logger = logging.getLogger(__name__)


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
    # index and hold the writer lock for the full duration; on the
    # 11M-row corpus that's ~25 s, dominant source of broker-side
    # cache.set stalls in production. Setting `analysis_limit` on
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


# Per-block opt-in flag. Toggled by `write_transaction()` so the
# `begin` event listener below knows whether the next BEGIN should
# be IMMEDIATE (writer lock acquired upfront) or DEFERRED (SQLAlchemy
# default; the connection only takes the writer lock on its first
# write statement).
_WRITE_TXN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "mimir_write_txn",
    default=False,
)

# Operator-facing label for the current write_transaction()'s slow
# log entry. Set inside write_transaction(label="..."); read at
# commit/rollback time. `None` means "unlabelled"; the slow log
# falls back to a sentinel string so the line is still grep-able.
_WRITE_TXN_LABEL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mimir_write_txn_label",
    default=None,
)

# Perf-counter timestamp of the last BEGIN IMMEDIATE inside a
# `write_transaction()` block on this task. Reset to None when the
# commit / rollback event fires and the slow-log decision is made,
# so a subsequent BEGIN inside the same `with write_transaction()`
# block starts a fresh measurement.
_WRITE_TXN_STARTED_AT: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "mimir_write_txn_started_at", default=None
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
        # Stamp the moment we acquired the writer lock so the
        # commit / rollback listeners below can compute elapsed
        # time and emit a WARNING when the transaction held the
        # lock longer than `settings.write_transaction_slow_log_ms`.
        # This is the operator's diagnostic signal for cross-
        # process writer-lock contention: a slow `write_transaction`
        # on the scheduler side correlates 1:1 with a slow broker
        # dispatch on the cache side.
        _WRITE_TXN_STARTED_AT.set(time.perf_counter())


@event.listens_for(engine, "commit")
def _log_slow_write_transaction_on_commit(conn) -> None:
    """Log a WARNING when a `write_transaction()` block held the
    writer lock longer than `Settings.write_transaction_slow_log_ms`.
    Fires on COMMIT (the typical path); see `_log_slow_write_transaction_on_rollback`
    for the failure path. No-op for transactions outside a
    `write_transaction()` block."""
    _emit_slow_write_log_if_needed(rolled_back=False)


@event.listens_for(engine, "rollback")
def _log_slow_write_transaction_on_rollback(conn) -> None:
    """Companion to `_log_slow_write_transaction_on_commit`. Logs the
    same way when a transaction terminates via ROLLBACK (e.g. on an
    exception that propagates out of the `with` block) rather than
    COMMIT, so an operator sees the writer-lock-hold duration
    regardless of outcome."""
    _emit_slow_write_log_if_needed(rolled_back=True)


def _emit_slow_write_log_if_needed(*, rolled_back: bool) -> None:
    started_at = _WRITE_TXN_STARTED_AT.get()
    if started_at is None:
        # No active write_transaction-marked BEGIN; nothing to log.
        # (This is the common case for non-write_transaction commits.)
        return
    # Clear immediately so a subsequent BEGIN in the same block
    # measures from its own start, and so we don't double-log if
    # commit + rollback both fire.
    _WRITE_TXN_STARTED_AT.set(None)
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    threshold_ms = settings.write_transaction_slow_log_ms
    if threshold_ms <= 0 or elapsed_ms < threshold_ms:
        return
    label = _WRITE_TXN_LABEL.get() or "(unlabelled)"
    verb = "slow rollback" if rolled_back else "slow"
    logger.warning(
        "write_transaction %s: label=%s held=%.1fms",
        verb,
        label,
        elapsed_ms,
    )


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
def write_transaction(label: str | None = None):
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

    `label` identifies the caller in the slow-write log line emitted
    when the transaction holds the writer lock longer than
    `Settings.write_transaction_slow_log_ms`. Caller convention is
    `<area>:<scope>`, e.g. `"ingest_inbox:lkml"`,
    `"backfill_canonicals"`, `"auto_analyze:lkml"`,
    `"broker:cache_set"`. Defaults to `None` → logged as
    `(unlabelled)`, callable but useless for triage.
    """
    label_token = _WRITE_TXN_LABEL.set(label)
    token = _WRITE_TXN.set(True)
    try:
        yield
    finally:
        _WRITE_TXN.reset(token)
        _WRITE_TXN_LABEL.reset(label_token)
