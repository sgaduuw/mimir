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

from sqlalchemy import text

from mimir.config import settings
from mimir.extensions import engine


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


def test_write_transaction_symbol_removed():
    """Phase 6b: write_transaction and the BEGIN-IMMEDIATE machinery are
    gone. The broker is the sole writer on its own engine, so no
    shared-engine BEGIN-IMMEDIATE promotion is needed."""
    import mimir.extensions as ext

    assert not hasattr(ext, "write_transaction"), (
        "write_transaction must be removed in Phase 6b"
    )


def test_analysis_limit_is_sqlite_default_0():
    """#817: ANALYZE reads every row. A cap is not a random sample, it
    records N + 1 for every big index (CONTEXT.md "ANALYZE sample
    size"), so nothing sets one and a connection must report SQLite's
    own default 0. Fails if a cap is reintroduced in `_sqlite_pragmas`."""
    assert _pragma("analysis_limit") == 0


# ---------------------------------------------------------------------------
# PRAGMA query_only gating on `settings.mimir_is_broker` (2.0.0 invariant)
# ---------------------------------------------------------------------------


def test_pragmas_listener_sets_query_only_when_not_broker(monkeypatch):
    """The 2.0.0 single-writer invariant: every non-broker process
    opens its SQLite connections with `PRAGMA query_only=1`, so any
    code path that escaped routing through the broker's WriteRPC
    surfaces immediately as `OperationalError: attempt to write a
    readonly database` rather than silently bypassing the serialised
    writer queue (CONTEXT.md "Single-writer invariant via the
    broker (v2.0.0+)").

    The listener `_sqlite_pragmas` decides per-connection based on
    `settings.mimir_is_broker`. The conftest sets MIMIR_IS_BROKER=true
    process-wide so the test process can write, but the runtime
    behavior the test pins is the OTHER branch (non-broker). Call
    the listener directly against a fresh raw sqlite3 connection so
    nothing about the shared engine pool's state matters.
    """
    import sqlite3

    from mimir.extensions import _sqlite_pragmas

    monkeypatch.setattr(settings, "mimir_is_broker", False)

    conn = sqlite3.connect(":memory:")
    try:
        _sqlite_pragmas(conn, None)
        cur = conn.cursor()
        cur.execute("PRAGMA query_only")
        result = cur.fetchone()[0]
    finally:
        conn.close()

    assert result == 1, (
        "non-broker connections must open with PRAGMA query_only=1 so "
        "accidental writes raise loudly instead of silently bypassing "
        "the broker's single-writer queue"
    )


def test_pragmas_listener_does_not_set_query_only_when_broker():
    """Inverse: when `settings.mimir_is_broker` is True (i.e. the
    broker process itself), the listener must NOT set query_only.
    The broker IS the writer; setting query_only would brick every
    write path on the broker. Conftest sets MIMIR_IS_BROKER=true at
    import time, so this is the steady-state path under the test
    harness.
    """
    import sqlite3

    from mimir.extensions import _sqlite_pragmas

    # Sanity check: conftest's process-wide setting puts us in the
    # broker branch by default. If this assertion fails, the
    # listener's gate condition is being tested against the wrong
    # baseline.
    assert settings.mimir_is_broker is True, (
        "test harness invariant: conftest sets MIMIR_IS_BROKER=true "
        "so the test process can act as the writer for fixtures"
    )

    conn = sqlite3.connect(":memory:")
    try:
        _sqlite_pragmas(conn, None)
        cur = conn.cursor()
        cur.execute("PRAGMA query_only")
        result = cur.fetchone()[0]
    finally:
        conn.close()

    assert result == 0, (
        "broker connections must NOT carry query_only=1; the broker IS "
        "the sole SQLite writer process"
    )
