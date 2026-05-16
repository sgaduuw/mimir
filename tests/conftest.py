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
        s.execute(delete(ArticleFile))   # FK to articles; clear before Article
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

    mimir.inboxes._INBOX_NAMES[:] = []

    with SessionLocal() as s:
        alpha = Inbox(
            name=TEST_INBOX_PRIMARY,
            mirror_path="/tmp/alpha",
            upstream_url="https://example.com/alpha",
        )
        beta = Inbox(
            name=TEST_INBOX_SECONDARY,
            mirror_path="/tmp/beta",
            upstream_url="https://example.com/beta",
        )
        s.add_all([alpha, beta])
        s.flush()

        # art1: alpha only
        # art2: beta only
        # art3: cross-posted (alpha + beta)
        # art4: alpha, replies to art1 (small thread)
        art1 = Article(
            message_id="art1@example.com", subject="hello alpha",
            author="Alice <alice@example.com>",
            date=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
            thread_parent=None, subject_normalized="hello alpha",
        )
        art2 = Article(
            message_id="art2@example.com", subject="hello beta",
            author="Bob <bob@example.com>",
            date=datetime(2024, 2, 1, 12, 0, tzinfo=timezone.utc),
            thread_parent=None, subject_normalized="hello beta",
        )
        art3 = Article(
            message_id="art3@example.com", subject="cross-posted note",
            author="Carol <carol@kernel.org>",
            date=datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc),
            thread_parent=None, subject_normalized="cross-posted note",
        )
        art4 = Article(
            message_id="art4@example.com", subject="Re: hello alpha",
            author="Dave <dave@example.com>",
            date=datetime(2024, 1, 2, 12, 0, tzinfo=timezone.utc),
            thread_parent="art1@example.com",
            subject_normalized="hello alpha",
        )
        s.add_all([art1, art2, art3, art4])
        s.flush()

        s.add_all([
            ArticleList(article_id=art1.id, inbox_id=alpha.id, epoch="0.git", commit_sha="aa" * 20),
            ArticleList(article_id=art2.id, inbox_id=beta.id,  epoch="0.git", commit_sha="bb" * 20),
            ArticleList(article_id=art3.id, inbox_id=alpha.id, epoch="0.git", commit_sha="cc" * 20),
            ArticleList(article_id=art3.id, inbox_id=beta.id,  epoch="0.git", commit_sha="cd" * 20),
            ArticleList(article_id=art4.id, inbox_id=alpha.id, epoch="0.git", commit_sha="dd" * 20),
        ])
        s.commit()

    mimir.inboxes.refresh_inbox_names()
    yield


@pytest.fixture
def seeded_db():
    """Return the bound SessionLocal so tests can open scoped
    sessions against the seeded test DB. The autouse `_reset_db`
    has already wiped + seeded by the time this fixture is asked
    for."""
    from mimir.extensions import SessionLocal
    return SessionLocal


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
