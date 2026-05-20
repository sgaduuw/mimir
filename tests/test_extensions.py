"""SQLite pragma contract for `mimir.extensions`.

The `connect` listener at `mimir/extensions.py` sets four pragmas on
every connection, WAL journaling, NORMAL sync, FK enforcement, and
the configured `busy_timeout`. These are load-bearing:

- WAL is what makes readers non-blocking during ingest writes.
- `foreign_keys=ON` is what makes the `ON DELETE SET NULL` /
  `ON DELETE CASCADE` clauses on `articles.canonical_inbox_id` and
  `article_lists` actually fire.
- `busy_timeout` is what rides out scheduler write windows; without
  it (the SQLite default is 0) transient contention surfaces as
  hard 500s.

A regression that broke the listener wiring (e.g. an SQLAlchemy
2.x update that changed `event.listens_for` semantics, or someone
swapping the engine for a fresh one without re-registering) would
only show up on a production lock incident. These tests assert the
contract directly so the regression surfaces in CI instead.
"""
from sqlalchemy import event, text

from mimir.config import settings
from mimir.extensions import SessionLocal, engine, write_transaction


def _pragma(name: str):
    """Read a PRAGMA's current value through a fresh connection. The
    listener fires on connect, so this is the contract under test."""
    with engine.connect() as conn:
        return conn.execute(text(f"PRAGMA {name}")).scalar()


def test_journal_mode_is_wal():
    """WAL is persistent in the SQLite header once written; we still
    re-issue on every connect for idempotence. A fresh connection
    must report `wal`."""
    assert _pragma("journal_mode").lower() == "wal"


def test_foreign_keys_enabled():
    """`PRAGMA foreign_keys=ON` returns 1 when enabled. Required for
    the `ON DELETE` clauses on `articles.canonical_inbox_id` and
    `article_lists.article_id` to fire."""
    assert _pragma("foreign_keys") == 1


def test_busy_timeout_matches_settings():
    """The listener wires `busy_timeout` from `Settings.sqlite_busy_timeout_ms`
    so coruscant can lengthen it via env without code changes."""
    assert _pragma("busy_timeout") == settings.sqlite_busy_timeout_ms


def test_synchronous_is_normal():
    """`synchronous=NORMAL` is the cost/safety trade WAL recommends:
    durable across application crashes, can lose the tail of an
    uncommitted write across an OS crash. FULL is the alternative;
    OFF would be unsafe. PRAGMA returns the integer level: NORMAL=1."""
    assert _pragma("synchronous") == 1


def test_pragmas_apply_per_connection():
    """The listener must fire on *every* new connection, not just the
    first. SQLAlchemy's connection pool keeps connections warm, but
    a fresh open (whether cold or after pool churn) must still get
    the pragmas, otherwise the contract degrades to "whichever
    connection the engine happened to hand out first." Open two
    fresh connections and verify both."""
    for _ in range(2):
        with engine.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar()
            fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
            bt = conn.execute(text("PRAGMA busy_timeout")).scalar()
        assert mode.lower() == "wal"
        assert fk == 1
        assert bt == settings.sqlite_busy_timeout_ms


# write_transaction(): BEGIN IMMEDIATE opt-in for long-running CLI
# write paths. Pins the contract so a regression that flipped the
# event-listener wiring (or dropped the contextvar guard) would surface
# in CI rather than as a production SQLITE_BUSY_SNAPSHOT under load.


def _capture_sql():
    """Install a `before_cursor_execute` listener that records every
    SQL statement the engine emits. Returns `(seen, install, remove)`."""
    seen: list[str] = []

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    def install():
        event.listen(engine, "before_cursor_execute", _on_exec)

    def remove():
        event.remove(engine, "before_cursor_execute", _on_exec)

    return seen, install, remove


def test_write_transaction_emits_begin_immediate():
    """Inside `write_transaction()`, every BEGIN must be IMMEDIATE so
    the connection acquires the writer lock at transaction start.
    The deferred default is what tripped SQLITE_BUSY_SNAPSHOT against
    the gunicorn cache writes in production: backfill SELECTs first,
    takes a snapshot, then INSERTs; another writer commits in
    between, and the snapshot upgrade is rejected (non-retryable
    via busy_timeout)."""
    seen, install, remove = _capture_sql()
    install()
    try:
        with write_transaction(), SessionLocal() as session:
            session.execute(text("SELECT 1"))
            session.commit()
    finally:
        remove()

    assert any("BEGIN IMMEDIATE" in s.upper() for s in seen), (
        f"expected BEGIN IMMEDIATE inside write_transaction(); SQL: {seen}"
    )


def test_default_outside_write_transaction_is_deferred():
    """Outside the helper, sessions begin DEFERRED (the SQLAlchemy
    default). Important to pin: read-only paths (web routes, cache
    reads) MUST NOT promote to IMMEDIATE, or every gunicorn worker
    would serialise on the writer lock."""
    seen, install, remove = _capture_sql()
    install()
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
            session.commit()
    finally:
        remove()

    assert not any("BEGIN IMMEDIATE" in s.upper() for s in seen), (
        f"plain SessionLocal() must not emit BEGIN IMMEDIATE; SQL: {seen}"
    )


def test_write_transaction_applies_to_every_transaction_in_block():
    """Each `session.commit()` ends one transaction; the next operation
    implicitly opens a new one. The contextvar gating must apply to
    *every* BEGIN in the block, not just the first, so a batched
    backfill (which commits every N rows) doesn't silently relapse
    to deferred mid-run."""
    seen, install, remove = _capture_sql()
    install()
    try:
        with write_transaction(), SessionLocal() as session:
            session.execute(text("SELECT 1"))
            session.commit()
            session.execute(text("SELECT 2"))
            session.commit()
    finally:
        remove()

    immediates = [s for s in seen if "BEGIN IMMEDIATE" in s.upper()]
    assert len(immediates) >= 2, (
        f"expected ≥2 BEGIN IMMEDIATE (one per transaction in the "
        f"block); got {len(immediates)}: {seen}"
    )


def test_write_transaction_resets_after_block():
    """The contextvar is reset on block exit. A subsequent session
    outside the block goes back to deferred BEGIN; otherwise a
    single backfill invocation would leak IMMEDIATE mode into the
    entire process for the rest of its lifetime, which would
    re-serialise the web tier on the writer lock."""
    with write_transaction(), SessionLocal() as session:
        session.execute(text("SELECT 1"))
        session.commit()

    seen, install, remove = _capture_sql()
    install()
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
            session.commit()
    finally:
        remove()

    assert not any("BEGIN IMMEDIATE" in s.upper() for s in seen), (
        "write_transaction() must not leak into subsequent sessions"
    )


def test_write_transaction_raises_busy_timeout():
    """Inside `write_transaction()`, the connection's busy_timeout
    is raised to `sqlite_busy_timeout_ms_writes` (default 60s).
    Default 5s is right for request handlers but too short for a
    backfill: BEGIN IMMEDIATE itself can fail with the recoverable
    SQLITE_BUSY if the writer lock is held when the backfill tries
    to acquire it, and a cache-write burst (after an archive_stats
    invalidation, every cold-miss render writes back) easily
    exceeds 5s of queue depth."""
    with write_transaction(), SessionLocal() as session:
        # Force a transaction so the begin listener fires and bumps
        # the pragma.
        session.execute(text("SELECT 1"))
        observed = session.execute(text("PRAGMA busy_timeout")).scalar()
        session.commit()
    assert observed == settings.sqlite_busy_timeout_ms_writes, (
        f"expected busy_timeout={settings.sqlite_busy_timeout_ms_writes} "
        f"inside write_transaction(); got {observed}"
    )


def test_busy_timeout_restored_on_pool_checkin():
    """The pool-`reset` listener restores busy_timeout to the
    web-tier default when a connection is returned to the pool.
    Without this, a write_transaction()-promoted connection going
    back to the pool would hand the higher value to whoever checks
    it out next (typically a web request handler), and the short-
    timeout contract for request handlers would silently drift."""
    # Burn a write_transaction() so a connection visits the bump
    # path and then returns to the pool with the reset applied.
    with write_transaction(), SessionLocal() as session:
        session.execute(text("SELECT 1"))
        session.commit()

    # Fresh checkout: the pool may hand back the same connection.
    # Either way, busy_timeout must be the default.
    with engine.connect() as conn:
        observed = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert observed == settings.sqlite_busy_timeout_ms, (
        f"pool checkin must restore busy_timeout to "
        f"{settings.sqlite_busy_timeout_ms}; got {observed}"
    )


# ---------------------------------------------------------------------------
# Slow-write logging contract for write_transaction(). The COMMIT and
# ROLLBACK listeners log a WARNING with the supplied label and the
# elapsed time the writer lock was held, when over
# `settings.write_transaction_slow_log_ms`. This is the operator-facing
# diagnostic for cross-process writer-lock contention: a slow write on
# the scheduler side correlates 1:1 with a slow broker dispatch on the
# cache side.
# ---------------------------------------------------------------------------


def test_write_transaction_slow_log_fires_on_commit(caplog, monkeypatch):
    """When a `write_transaction(label=...)` block holds the writer
    lock longer than the threshold and commits cleanly, the COMMIT
    listener emits a WARNING tagged with the label and elapsed
    time."""
    import logging
    import time as _time

    # Trip the threshold trivially: 0ms means "every write_transaction
    # logs". 1ms also works but is racy in a fast test environment.
    monkeypatch.setattr(settings, "write_transaction_slow_log_ms", 1)
    caplog.set_level(logging.WARNING, logger="mimir.extensions")

    with write_transaction("test:slow_commit"), SessionLocal() as session:
        session.execute(text("SELECT 1"))
        # Sleep beyond the threshold so the elapsed measurement is
        # comfortably over 1ms regardless of CI jitter.
        _time.sleep(0.05)
        session.commit()

    slow = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "write_transaction slow" in r.getMessage()
    ]
    assert slow, (
        "expected a slow-write WARNING; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
    msg = slow[0].getMessage()
    assert "label=test:slow_commit" in msg
    assert "held=" in msg


def test_write_transaction_slow_log_fires_on_rollback(caplog, monkeypatch):
    """An exception that propagates out of the `write_transaction()`
    block fires ROLLBACK at the SessionLocal exit; the ROLLBACK
    listener emits the same slow-write WARNING tagged as a
    `slow rollback` so the operator sees lock-hold duration even
    when the transaction failed."""
    import logging
    import time as _time

    monkeypatch.setattr(settings, "write_transaction_slow_log_ms", 1)
    caplog.set_level(logging.WARNING, logger="mimir.extensions")

    class _Boom(RuntimeError):
        pass

    try:
        with write_transaction("test:slow_rollback"), SessionLocal() as session:
            session.execute(text("SELECT 1"))
            _time.sleep(0.05)
            raise _Boom()
    except _Boom:
        pass

    slow = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "write_transaction slow rollback" in r.getMessage()
    ]
    assert slow, (
        "expected a slow-rollback WARNING; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
    assert "label=test:slow_rollback" in slow[0].getMessage()


def test_write_transaction_below_threshold_is_silent(caplog, monkeypatch):
    """When the block completes faster than the threshold, no
    WARNING is emitted. Healthy commits land in single-digit ms,
    so the default 1000ms threshold is "this is interesting, not
    noise"."""
    import logging

    monkeypatch.setattr(settings, "write_transaction_slow_log_ms", 60_000)
    caplog.set_level(logging.WARNING, logger="mimir.extensions")

    with write_transaction("test:fast"), SessionLocal() as session:
        session.execute(text("SELECT 1"))
        session.commit()

    slow = [
        r for r in caplog.records
        if "write_transaction slow" in r.getMessage()
    ]
    assert not slow, "fast commits must not log a slow-write WARNING"


def test_write_transaction_zero_threshold_disables_log(caplog, monkeypatch):
    """`write_transaction_slow_log_ms = 0` disables the slow-write
    log entirely. Operators who don't want the line can opt out."""
    import logging
    import time as _time

    monkeypatch.setattr(settings, "write_transaction_slow_log_ms", 0)
    caplog.set_level(logging.WARNING, logger="mimir.extensions")

    with write_transaction("test:disabled"), SessionLocal() as session:
        session.execute(text("SELECT 1"))
        _time.sleep(0.02)
        session.commit()

    slow = [
        r for r in caplog.records
        if "write_transaction slow" in r.getMessage()
    ]
    assert not slow, "threshold=0 must disable slow-write log"


def test_write_transaction_unlabelled_default(caplog, monkeypatch):
    """`write_transaction()` with no label still logs when slow,
    but with the sentinel `(unlabelled)` so the line is grep-able
    and obviously incomplete (callers should always pass a
    label)."""
    import logging
    import time as _time

    monkeypatch.setattr(settings, "write_transaction_slow_log_ms", 1)
    caplog.set_level(logging.WARNING, logger="mimir.extensions")

    with write_transaction(), SessionLocal() as session:
        session.execute(text("SELECT 1"))
        _time.sleep(0.05)
        session.commit()

    slow = [
        r for r in caplog.records
        if "write_transaction slow" in r.getMessage()
    ]
    assert slow, "expected a slow-write WARNING"
    assert "label=(unlabelled)" in slow[0].getMessage()
