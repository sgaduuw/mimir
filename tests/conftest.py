"""Test-suite-wide setup.

Pointing the entire suite at a temp SQLite removes the fragile
"swap engines mid-test" pattern: every consumer of the bound
SessionLocal, anywhere in the app, sees the test DB by virtue of
`DATABASE_URL` being set BEFORE any `mimir` module is imported.

Fixture stack:

* Module-level setup (runs before any mimir import):
    - mkdtemp + DATABASE_URL=sqlite:///<tmp>
    - SECRET_KEY=<dummy long enough for pydantic>

* Session-scoped, autouse `_migrate_db`:
    - alembic upgrade head against the temp DB. Once per session.

* Function-scoped, autouse `_reset_db`:
    - DELETE every row from inboxes/articles/article_lists/
      ingest_state/cache to give each test a clean slate, then
      seed the small fixture dataset (two inboxes, four articles,
      one cross-post). ~10 ms per test.

* Function-scoped `seeded_db` (compatibility shim):
    - Just returns SessionLocal. Existing tests that took the
      fixture as an argument keep working unchanged.

* Function-scoped `client` and `inbox_name`:
    - Shared across test files that need a Flask test client and
      a known seeded inbox name. Replaces the per-file fixtures
      in test_routes.py.
"""

import os
import tempfile

# CRITICAL: env vars must be set BEFORE any `mimir` import. pytest
# loads conftest.py before test modules, so as long as nothing here
# at module level imports mimir, settings will read these values.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="mimir-test-")
_TEST_DB_PATH = os.path.join(_TEST_DB_DIR, "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-real-1234567890abc")
os.environ.setdefault("FLASK_DEBUG", "false")
# The test process IS the writer: fixtures need alembic + seed
# direct against the test SQLite. Setting MIMIR_IS_BROKER=true at
# import time keeps `_sqlite_pragmas` from issuing `PRAGMA
# query_only=1` on every connection. In-process broker fixtures
# (`broker_running`) still work because broker-handler self-RPC
# avoidance uses a thread-local flag (`_HANDLER_TLS`), independent
# of this process-wide setting.
os.environ["MIMIR_IS_BROKER"] = "true"

import pytest  # noqa: E402

# Test seed constants, exposed so tests can reference them rather
# than re-typing string literals.
TEST_INBOX_PRIMARY = "alpha"
TEST_INBOX_SECONDARY = "beta"


@pytest.fixture(scope="session", autouse=True)
def _migrate_db():
    """alembic upgrade head once for the whole test session."""
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
    command.upgrade(cfg, "head")
    yield
    # Dispose pooled connections explicitly so ResourceWarnings
    # don't surface at process exit (SQLAlchemy's pool holds idle
    # sqlite3 connections; without dispose() they get GC'd after
    # the interpreter starts tearing down, which the cpython
    # sqlite3 module reports as "unclosed database").
    from mimir.extensions import engine

    engine.dispose()
    # Tempdir is left for the OS to reap; no need to teardown.


# References to the session broker's pool + writer, populated by
# `_session_broker` at session start so the autouse re-registration
# fixture can reinstate them after legacy tests that clear context.
_SESSION_BROKER_REFS: dict = {}


@pytest.fixture(scope="session", autouse=True)
def _session_broker(_migrate_db):
    """One in-process broker for the whole test session.

    Post-2.0.0 CLI commands route every write through the broker
    via `get_broker_client()`; without a real broker on the socket
    they fail with `BrokerUnavailable` and the click invocation
    exits non-zero. This fixture starts one broker for the session
    and points `settings.broker_socket_path` at its socket.

    Self-bootstrap (alembic + bootstrap_inboxes + post-migrate
    ANALYZE) is skipped via pre-touched sentinels: the
    `_migrate_db` fixture already ran alembic, `_reset_db` seeds
    inboxes per-test, and tests don't want a session-start
    ANALYZE pass.

    Per-test broker fixtures (`broker_running` in
    `tests/test_broker/_helpers.py`) still work; they spin up a
    SECOND broker on a different socket for tests that need
    isolated broker behaviour, and the calling tests monkeypatch
    `settings.broker_socket_path` to point at it for their
    duration."""
    import threading
    from pathlib import Path

    # Lazy import to keep conftest's import-time graph lean; the
    # broker package pulls a lot of mimir in.
    from mimir.broker.server import build_server
    from mimir.config import settings

    # /tmp because macOS's AF_UNIX 104-byte limit doesn't admit
    # the pytest tmp_path under /var/folders/.../T/.
    sock_dir = Path(tempfile.mkdtemp(prefix="mimir-bsess-", dir="/tmp"))
    sock_path = sock_dir / "broker.sock"

    # Pre-touch the self-bootstrap sentinels so `build_server`'s
    # _migrate_if_needed / _bootstrap_inboxes_if_needed /
    # _post_migrate_analyze_if_needed all short-circuit.
    (sock_dir / ".migrated").touch()
    (sock_dir / ".bootstrapped").touch()
    (sock_dir / ".broker_initial_analyze").touch()

    server = build_server(sock_path)
    server.writer.start()
    # Register the session broker's pool + writer so direct in-process
    # callers (test helpers calling ingest_epoch/ingest_inbox without
    # going through RPC) can resolve `_context.get_active_pool()` and
    # `_context.get_active_writer()`. Safe for test-thread cache.set
    # calls because cache.set gates the writer path on the broker
    # handler thread-local flag, not on the global context.
    from mimir.broker import _context as broker_context

    broker_context.set_active(server.read_pool, server.writer)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
        name="pytest-session-broker",
    )
    thread.start()

    prev = settings.broker_socket_path
    settings.broker_socket_path = sock_path
    # Expose the pool+writer for the autouse re-registration fixture
    # so legacy tests that wipe the active context (without saving)
    # can have it reinstated for the next test.
    _SESSION_BROKER_REFS["pool"] = server.read_pool
    _SESSION_BROKER_REFS["writer"] = server.writer
    try:
        yield sock_path
    finally:
        settings.broker_socket_path = prev
        broker_context.clear_active()
        server.stop_event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        server.writer.stop(timeout=10)
        server.read_pool.close()
        if sock_path.exists():
            sock_path.unlink()
        # Sentinels + dir cleanup.
        for name in (".migrated", ".bootstrapped", ".broker_initial_analyze"):
            (sock_dir / name).unlink(missing_ok=True)
        sock_dir.rmdir()


@pytest.fixture
def broker_active(seeded_db):
    """Per-test fixture: registers a fresh ReadSessionPool + WriterThread
    as the active broker context for code that reaches _context accessors
    (ingest_inbox, ingest_epoch, update_mainline, warm handlers). Cleared
    after each test so the registration never leaks into subsequent tests.

    This is the correct isolation boundary for Phase 3b (and 3a) tests
    that call the context-dependent code paths. Session-scoped registration
    would leak stale state into unrelated tests.

    Saves and restores any pre-existing active context so the session-scoped
    _session_broker fixture's registration survives across test files."""
    from mimir.broker import _context
    from mimir.broker.pools import ReadSessionPool
    from mimir.broker.writes import WriterThread

    saved_pool = _context._active_pool
    saved_writer = _context._active_writer

    pool = ReadSessionPool.from_settings()
    writer = WriterThread.from_settings()
    writer.start()
    try:
        _context.set_active(pool, writer)
        yield
    finally:
        writer.stop(timeout=10)
        pool.close()
        # Restore the previous context (e.g. the session broker's
        # registration) so subsequent tests are unaffected.
        if saved_pool is not None and saved_writer is not None:
            _context.set_active(saved_pool, saved_writer)
        else:
            _context.clear_active()


@pytest.fixture(autouse=True)
def _ensure_session_broker_context(_session_broker):
    """Re-register the session broker's pool + writer in `_context`
    after each test. Several legacy test fixtures (test_cli_maintainers,
    test_mainline, etc.) call `_context.clear_active()` on teardown
    without saving/restoring; this autouse fixture closes that hole
    by reinstating the session broker's registration so the next test
    finds a valid context."""
    from mimir.broker import _context as broker_context

    yield
    if broker_context._active_pool is None or broker_context._active_writer is None:
        broker_context.set_active(
            _SESSION_BROKER_REFS["pool"], _SESSION_BROKER_REFS["writer"]
        )


@pytest.fixture(autouse=True)
def _reset_db():
    """Wipe + reseed before every test so each one starts from a
    known baseline. ~10 ms on a fresh DB."""
    from datetime import datetime, timezone

    from sqlalchemy import delete

    from mimir.extensions import SessionLocal
    import mimir.inboxes
    from mimir.models import (
        Article,
        ArticleFile,
        ArticleList,
        ArticleTrailer,
        CacheEntry,
        Inbox,
        InboxAddressObservation,
        IngestState,
        MainlineCommit,
        MainlineState,
        ParseFailure,
        Subsystem,
    )

    with SessionLocal() as s:
        # FK ON DELETE CASCADE handles article_lists and ingest_state
        # for inbox deletes, and articles cascade-deletes article_lists
        # for article deletes, but explicit is cheaper on a tiny DB
        # and immune to FK-order surprises.
        s.execute(delete(IngestState))
        s.execute(delete(ArticleFile))  # FK to articles; clear before Article
        s.execute(delete(ArticleTrailer))  # FK to articles
        s.execute(delete(ArticleList))
        s.execute(delete(Article))
        s.execute(delete(ParseFailure))
        s.execute(delete(InboxAddressObservation))
        s.execute(delete(Inbox))
        s.execute(delete(CacheEntry))
        s.execute(delete(MainlineCommit))
        s.execute(delete(MainlineState))
        # Subsystem cascades to subsystem_paths + subsystem_maintainers
        # via ON DELETE CASCADE (FKs declared on the child tables).
        s.execute(delete(Subsystem))
        s.commit()

    # Re-seed robots_rules to migration defaults so tests that mutate
    # the table start each case from the same `*` baseline.
    import mimir.robots

    mimir.robots.reset_rules()

    mimir.inboxes._INBOX_NAMES[:] = []

    with SessionLocal() as s:
        # `last_article_date` mirrors what real ingest would set, so
        # `archive_stats.last_date` (which now overlays this column
        # at request time, see #216) matches the seeded MAX(date).
        # alpha: max(art1=2024-01-01, art3=2024-03-01, art4=2024-01-02);
        # beta:  max(art2=2024-02-01, art3=2024-03-01).
        alpha = Inbox(
            name=TEST_INBOX_PRIMARY,
            mirror_path="/tmp/alpha",
            upstream_url="https://example.com/alpha",
            last_article_date=datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc),
        )
        beta = Inbox(
            name=TEST_INBOX_SECONDARY,
            mirror_path="/tmp/beta",
            upstream_url="https://example.com/beta",
            last_article_date=datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc),
        )
        s.add_all([alpha, beta])
        s.flush()

        # art1: alpha only
        # art2: beta only
        # art3: cross-posted (alpha + beta)
        # art4: alpha, replies to art1 (small thread)
        art1 = Article(
            message_id="art1@example.com",
            subject="hello alpha",
            author="Alice <alice@example.com>",
            date=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized="hello alpha",
        )
        art2 = Article(
            message_id="art2@example.com",
            subject="hello beta",
            author="Bob <bob@example.com>",
            date=datetime(2024, 2, 1, 12, 0, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized="hello beta",
        )
        art3 = Article(
            message_id="art3@example.com",
            subject="cross-posted note",
            author="Carol <carol@kernel.org>",
            date=datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized="cross-posted note",
        )
        art4 = Article(
            message_id="art4@example.com",
            subject="Re: hello alpha",
            author="Dave <dave@example.com>",
            date=datetime(2024, 1, 2, 12, 0, tzinfo=timezone.utc),
            thread_parent="art1@example.com",
            subject_normalized="hello alpha",
        )
        s.add_all([art1, art2, art3, art4])
        s.flush()

        s.add_all(
            [
                ArticleList(
                    article_id=art1.id,
                    inbox_id=alpha.id,
                    epoch="0.git",
                    commit_sha="aa" * 20,
                ),
                ArticleList(
                    article_id=art2.id,
                    inbox_id=beta.id,
                    epoch="0.git",
                    commit_sha="bb" * 20,
                ),
                ArticleList(
                    article_id=art3.id,
                    inbox_id=alpha.id,
                    epoch="0.git",
                    commit_sha="cc" * 20,
                ),
                ArticleList(
                    article_id=art3.id,
                    inbox_id=beta.id,
                    epoch="0.git",
                    commit_sha="cd" * 20,
                ),
                ArticleList(
                    article_id=art4.id,
                    inbox_id=alpha.id,
                    epoch="0.git",
                    commit_sha="dd" * 20,
                ),
            ]
        )
        s.commit()

    mimir.inboxes.refresh_inbox_names()
    yield


def linus_tree(repo_path) -> dict:
    """Return a settings.trees dict containing only the linus entry
    pointing at `repo_path`. Used by tests that monkeypatch
    settings.trees to scope update_mainline to a single local repo
    (no network round-trips, no other-tree cadence logic).

    `url` points at a placeholder HTTPS target that passes URL
    validation; since tests all pass `--skip-fetch`, _ensure_tree
    never attempts a network round-trip.

    `walk_every_seconds=0` disables the cadence gate so repeated
    calls within a single test always execute (the gate checks
    `now - last_walked_at < walk_every_seconds`; zero means always
    due). Without this, the first call would write `last_walked_at`
    and the second call in the same test would be skipped by the
    gate, breaking tests that exercise two-tick sequences."""
    from mimir.config import TreeConfig

    return {
        "linus": TreeConfig(
            url="https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
            path=repo_path,
            walk_every_seconds=0,
        )
    }


@pytest.fixture
def seeded_db():
    """Return the bound SessionLocal so tests can open scoped
    sessions against the seeded test DB. The autouse `_reset_db`
    has already wiped + seeded by the time this fixture is asked
    for."""
    from mimir.extensions import SessionLocal

    return SessionLocal


@pytest.fixture
def session():
    """Yield an open SQLAlchemy session for the test DB. The
    session is closed after the test; the autouse `_reset_db`
    has already wiped + reseeded before this fixture is entered.

    Use this when a test needs to pass a live session directly
    to a service function rather than opening its own with
    `with seeded_db() as s:`."""
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        yield s


@pytest.fixture
def client():
    """Flask test client. Function-scoped so the security.txt
    monkeypatch tests in test_routes don't bleed into other
    test_routes tests through a shared module-scoped app."""
    from mimir import create_app

    return create_app().test_client()


@pytest.fixture
def inbox_name():
    """The seeded primary inbox name. Replaces test_routes.py's
    module-scoped fixture; lets the Message-ID lookup tests find a
    real article in the test DB."""
    return TEST_INBOX_PRIMARY


# Pinned mid-day UTC moment, well clear of any midnight boundary
# so a frozen-window test can compute "today" / "yesterday" / etc.
# from it without straddling a date rollover. The 12:00 picks the
# middle of the day; the date is arbitrary but stable.
FROZEN_NOW = "2024-06-15 12:00:00"


@pytest.fixture
def frozen_clock():
    """Freeze `datetime.now()` / `date.today()` at a deterministic
    mid-day UTC moment. Use for any test that compares a value derived
    from wall-clock `now()` on the test side against a value the
    handler / helper derives independently from its own `now()` -- if
    the two calls straddle UTC midnight the strings won't match.

    Yields the freezegun controller so tests that want to move time
    forward (e.g. cache-expiry checks) can `frozen_clock.move_to(...)`.
    """
    from freezegun import freeze_time

    with freeze_time(FROZEN_NOW) as ft:
        yield ft
