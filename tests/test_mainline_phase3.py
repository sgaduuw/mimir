"""Phase 3 tests for mimir.mainline batched writer dispatch.

These pin the new batch shape introduced by the broker two-pool
restructure (`_claude/specs/2026-05-29-broker-two-pool-design.md`).
Run alongside the existing test_mainline tests; kept in a separate
file so the Phase 3 PR audit is easy."""

import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from mimir.broker.writes import WriteOp, WriterThread
from mimir.config import settings
from mimir.mainline import _submit_mainline_batch


def _row(commit_sha: str, message_id: str, tree_name: str = "linus"):
    return {
        "commit_sha": commit_sha,
        "message_id": message_id,
        "tree_name": tree_name,
        "committed_at": datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc),
    }


def test_submit_mainline_batch_inserts_rows_via_writer(seeded_db):
    """_submit_mainline_batch composes a WriteOp wrapping the
    INSERT OR IGNORE for a batch of (commit_sha, message_id) pairs,
    submits it to the writer, and the rows appear after the writer
    drains."""
    writer = WriterThread.from_settings()
    writer.start()
    try:
        batch = [
            _row("a" * 40, "msg-a@kernel.org"),
            _row("b" * 40, "msg-b@kernel.org"),
        ]
        future = _submit_mainline_batch(writer, "linus", batch)
        future.result(timeout=5)

        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            count = c.execute(
                text(
                    "SELECT COUNT(*) FROM mainline_commits "
                    "WHERE tree_name = 'linus' AND commit_sha IN (:a, :b)"
                ),
                {"a": "a" * 40, "b": "b" * 40},
            ).scalar_one()
            assert count == 2
            # Commit the implicit read transaction before starting the
            # cleanup DELETE; SQLAlchemy 2.0 autobegin means the SELECT
            # above already opened a transaction on this connection.
            c.commit()
            c.execute(
                text(
                    "DELETE FROM mainline_commits "
                    "WHERE tree_name = 'linus' AND commit_sha IN (:a, :b)"
                ),
                {"a": "a" * 40, "b": "b" * 40},
            )
            c.commit()
        engine.dispose()
    finally:
        writer.stop(timeout=5)


def test_submit_mainline_batch_is_idempotent_on_replay(seeded_db):
    """If a batch is submitted twice (e.g. the next tick re-walks
    after a crash), the on_conflict_do_nothing makes the second
    submission a no-op rather than a constraint error.
    Verified by re-submitting the same batch and asserting row
    count stays at the original size."""
    writer = WriterThread.from_settings()
    writer.start()
    try:
        batch = [
            _row("c" * 40, "msg-c@kernel.org"),
        ]
        _submit_mainline_batch(writer, "linus", batch).result(timeout=5)
        # Second submission: should not raise; should be a no-op.
        _submit_mainline_batch(writer, "linus", batch).result(timeout=5)

        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            count = c.execute(
                text(
                    "SELECT COUNT(*) FROM mainline_commits "
                    "WHERE tree_name = 'linus' AND commit_sha = :s"
                ),
                {"s": "c" * 40},
            ).scalar_one()
            assert count == 1
            # Commit the implicit read transaction before starting the
            # cleanup DELETE; same SQLAlchemy 2.0 autobegin reason as
            # the first test.
            c.commit()
            c.execute(
                text(
                    "DELETE FROM mainline_commits "
                    "WHERE tree_name = 'linus' AND commit_sha = :s"
                ),
                {"s": "c" * 40},
            )
            c.commit()
        engine.dispose()
    finally:
        writer.stop(timeout=5)


def test_submit_mainline_batch_empty_batch_is_noop(seeded_db):
    """Empty batch returns a pre-resolved future without submitting
    anything to the writer (so the writer doesn't get spurious
    no-op WriteOps that just waste a transaction)."""
    writer = WriterThread.from_settings()
    writer.start()
    try:
        future = _submit_mainline_batch(writer, "linus", [])
        # Future should resolve immediately; .result() with timeout=0
        # should work (would block if a real submit happened).
        assert future.done()
        assert future.result() is None
    finally:
        writer.stop(timeout=5)


def test_submit_mainline_cursor_update_advances_state_via_writer(seeded_db):
    """_submit_mainline_cursor_update composes a WriteOp wrapping the
    MainlineState upsert for a tree's commits_walked_to_sha cursor,
    submits it to the writer, and the state row reflects the new
    SHA after the writer drains.

    UPSERT semantics: works whether the row exists or not.
    """
    from sqlalchemy import create_engine, text

    from mimir.broker.writes import WriterThread
    from mimir.config import settings
    from mimir.mainline import _submit_mainline_cursor_update

    writer = WriterThread.from_settings()
    writer.start()
    try:
        # First update: row may or may not exist for tree "linus"
        # from a prior test; either way the upsert should leave the
        # cursor at "d" * 40.
        future = _submit_mainline_cursor_update(writer, "linus", "d" * 40)
        future.result(timeout=5)

        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            sha = c.execute(
                text(
                    "SELECT commits_walked_to_sha FROM mainline_state "
                    "WHERE tree_name = 'linus'"
                )
            ).scalar_one_or_none()
            assert sha == "d" * 40
            c.commit()

            # Reset for other tests: set cursor back to NULL.
            c.execute(
                text(
                    "UPDATE mainline_state SET commits_walked_to_sha = NULL "
                    "WHERE tree_name = 'linus'"
                )
            )
            c.commit()
        engine.dispose()
    finally:
        writer.stop(timeout=5)


def test_submit_mainline_cursor_update_creates_row_when_missing(seeded_db):
    """If MainlineState has no row for this tree yet, the upsert
    creates one with the cursor set. Covers the "new tree" boot
    case where MainlineState may be empty."""
    from sqlalchemy import create_engine, text

    from mimir.broker.writes import WriterThread
    from mimir.config import settings
    from mimir.mainline import _submit_mainline_cursor_update

    writer = WriterThread.from_settings()
    writer.start()
    engine = create_engine(settings.database_url, future=True)
    try:
        # Ensure no row exists for tree "phase3-new".
        with engine.connect() as c:
            c.execute(text("DELETE FROM mainline_state WHERE tree_name = 'phase3-new'"))
            c.commit()

        future = _submit_mainline_cursor_update(writer, "phase3-new", "e" * 40)
        future.result(timeout=5)

        with engine.connect() as c:
            sha = c.execute(
                text(
                    "SELECT commits_walked_to_sha FROM mainline_state "
                    "WHERE tree_name = 'phase3-new'"
                )
            ).scalar_one()
            assert sha == "e" * 40
            c.commit()

            # Cleanup.
            c.execute(text("DELETE FROM mainline_state WHERE tree_name = 'phase3-new'"))
            c.commit()
    finally:
        engine.dispose()
        writer.stop(timeout=5)


def test_mid_walk_cursor_failure_leaves_cursor_at_old_position(
    seeded_db, tmp_path, monkeypatch
):
    """Phase 3 contract: a crash between batch N and the cursor
    submission leaves the cursor at its old position, so the next
    tick re-walks from there. on_conflict_do_nothing on
    (commit_sha, message_id) makes that replay a no-op for the
    batches that committed pre-crash.

    The test injects a failing _submit_mainline_cursor_update,
    asserts that batches committed but the cursor stayed at NULL,
    then replays the walk for real and asserts no duplicate rows
    and the cursor lands at HEAD."""
    from concurrent.futures import Future

    import sqlalchemy as sa
    from dulwich.objects import Commit, Tree
    from dulwich.repo import Repo
    from sqlalchemy import create_engine

    import mimir.mainline as mainline_mod
    from mimir.broker.writes import WriterThread
    from mimir.config import settings
    from mimir.mainline import walk_commits
    from mimir.models import MainlineCommit, MainlineState

    # Build a small bare repo with 5 commits that all carry Link: trailers.
    # batch_size=2 gives us 2 full batches (commits 1+2, commits 3+4) and
    # one tail batch (commit 5). The cursor update fires after the tail
    # batch (the injection point).
    repo_path = tmp_path / "crash-test.git"
    repo = Repo.init_bare(str(repo_path), mkdir=True)

    def _build(repo: Repo, msg: bytes, parent: bytes | None = None) -> bytes:
        tree = Tree()
        repo.object_store.add_object(tree)
        commit = Commit()
        commit.tree = tree.id
        commit.parents = [parent] if parent else []
        commit.author = commit.committer = b"test <t@x>"
        commit.commit_time = commit.author_time = 1700000000
        commit.commit_timezone = commit.author_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = msg
        repo.object_store.add_object(commit)
        repo.refs[b"HEAD"] = commit.id
        return commit.id

    prev = None
    for i in range(1, 6):
        prev = _build(
            repo,
            f"Fix {i}.\n\nLink: https://lore.kernel.org/r/m{i}@test\n".encode(),
            parent=prev,
        )

    tree_name = "crash-test"

    writer = WriterThread.from_settings()
    writer.start()
    try:
        # Walk 1: inject a failing cursor helper. The batch WriteOps
        # have already committed before the cursor fires (per Phase 3's
        # "cursor is the final WriteOp" design), so some MainlineCommit
        # rows should be present after the exception propagates.
        def _failing_cursor(w, tn, last_sha):
            f: Future = Future()
            f.set_exception(RuntimeError("simulated crash before cursor commit"))
            return f

        monkeypatch.setattr(
            mainline_mod, "_submit_mainline_cursor_update", _failing_cursor
        )

        import pytest as _pytest

        with _pytest.raises(RuntimeError, match="simulated crash"):
            with seeded_db() as session:
                walk_commits(
                    session,
                    repo_path,
                    tree_name=tree_name,
                    writer=writer,
                    batch_size=2,
                    branch="HEAD",
                )

        # State A: some MainlineCommit rows committed (the batches before
        # the cursor failed), but cursor still at the pre-walk value (NULL).
        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            rows_after_crash = c.execute(
                sa.select(MainlineCommit.commit_sha).where(
                    MainlineCommit.tree_name == tree_name
                )
            ).all()
            state_after_crash = c.execute(
                sa.select(MainlineState.commits_walked_to_sha).where(
                    MainlineState.tree_name == tree_name
                )
            ).scalar_one_or_none()
            c.commit()

        assert len(rows_after_crash) > 0, (
            "some batches should have committed before the cursor failure"
        )
        assert state_after_crash is None, (
            "cursor must NOT have advanced if the final WriteOp failed"
        )

        # Walk 2: restore the real cursor helper and replay. The already-
        # committed MainlineCommit rows are absorbed by on_conflict_do_nothing;
        # the cursor should advance to HEAD.
        monkeypatch.undo()

        with seeded_db() as session:
            walk_commits(
                session,
                repo_path,
                tree_name=tree_name,
                writer=writer,
                batch_size=2,
                branch="HEAD",
            )

        with engine.connect() as c:
            rows_after_replay = c.execute(
                sa.select(MainlineCommit.commit_sha).where(
                    MainlineCommit.tree_name == tree_name
                )
            ).all()
            state_after_replay = c.execute(
                sa.select(MainlineState.commits_walked_to_sha).where(
                    MainlineState.tree_name == tree_name
                )
            ).scalar_one_or_none()
            c.commit()

        # No duplicates: on_conflict_do_nothing absorbed the already-
        # committed batches. The cursor advanced to HEAD.
        assert len(rows_after_replay) == 5, (
            f"expected exactly 5 rows (one per Link: commit), got {len(rows_after_replay)}"
        )
        assert state_after_replay is not None, (
            "cursor should have advanced on the successful replay"
        )

        # Cleanup so other tests start clean.
        with engine.connect() as c:
            c.execute(
                sa.delete(MainlineCommit).where(MainlineCommit.tree_name == tree_name)
            )
            c.execute(
                sa.delete(MainlineState).where(MainlineState.tree_name == tree_name)
            )
            c.commit()
        engine.dispose()
    finally:
        writer.stop(timeout=10)


def test_update_mainline_uses_writer_thread_via_active_context(seeded_db, monkeypatch):
    """update_mainline should dispatch its writes through the active
    writer thread, NOT through write_transaction(). The walk-commits
    path inside the per-tree loop should not call write_transaction."""
    from mimir.broker import _context
    from mimir.broker.pools import ReadSessionPool
    from mimir.broker.writes import WriterThread

    # Monkeypatch write_transaction so a regression to it fails LOUDLY.
    # The 'mimir.mainline' namespace is what update_mainline reaches
    # write_transaction through (it imports it at module top).
    import mimir.mainline as mainline_mod

    if hasattr(mainline_mod, "write_transaction"):

        def explode(*a, **kw):
            raise AssertionError(
                "update_mainline must dispatch via WriterThread.submit, "
                "not via write_transaction. Phase 3 of the two-pool "
                "restructure routes mainline writes through the writer "
                "thread; if this assertion fires, the walker has "
                "regressed."
            )

        monkeypatch.setattr(mainline_mod, "write_transaction", explode)

    # Also patch the canonical source in case the function gets
    # imported differently in the future.
    monkeypatch.setattr("mimir.extensions.write_transaction", explode)

    pool = ReadSessionPool.from_settings()
    writer = WriterThread.from_settings()
    writer.start()

    try:
        _context.set_active(pool, writer)
        from mimir.mainline import update_mainline

        # skip_fetch=True: no subprocess.run (no clone/fetch).
        # skip_maintainers=True: load_maintainers (still uses
        # write_transaction internally, that's Phase 3b's job)
        # never runs.
        # skip_commits=True: walk_commits never runs, but
        # update_mainline's per-tree iteration still executes.
        # If the cadence-gate code calls write_transaction, this
        # test catches it; if it uses plain SessionLocal (the
        # last_walked_at write), the test passes.
        result = update_mainline(
            skip_fetch=True,
            skip_maintainers=True,
            skip_commits=True,
        )
        # The point isn't the return value; it's that the explode
        # callback never fired.
        assert result is not None
    finally:
        _context.clear_active()
        writer.stop(timeout=10)
        pool.close()


def test_cache_writes_drain_between_mainline_batches(seeded_db, tmp_path):
    """Phase 3 contract: while walk_commits dispatches batches through
    the writer, concurrent cache-set writes drain between batches
    instead of head-of-line-stalling for the full walk.

    Pre-Phase 3 held the writer lock in one continuous transaction for
    the entire walk. Phase 3 turns that into N short per-batch
    transactions, so cache.set-shaped WriteOps submitted from other
    threads see only one batch worth of lock hold, not the whole walk.

    Asserts: max per-cache-write wall time < 5 s (generous; on a
    healthy laptop the real numbers are tens of ms). The point is the
    absence of head-of-line stalls, not a wall-time speedup."""
    from dulwich.objects import Commit, Tree
    from dulwich.repo import Repo

    from mimir.mainline import walk_commits

    # --- build a 20-commit bare repo with Link: trailers -----------------
    # batch_size=5 gives 4 batches of 5 commits each.  Each batch's
    # WriteOp commits independently, releasing the writer lock before the
    # next batch starts.  The gap between batches is where concurrent
    # cache-set WriteOps can drain.
    repo_path = tmp_path / "ft-stress.git"
    repo = Repo.init_bare(str(repo_path), mkdir=True)

    def _build_commit(repo: Repo, msg: bytes, parent: bytes | None = None) -> bytes:
        tree = Tree()
        repo.object_store.add_object(tree)
        commit = Commit()
        commit.tree = tree.id
        commit.parents = [parent] if parent else []
        commit.author = commit.committer = b"test <t@x>"
        commit.commit_time = commit.author_time = 1700000000
        commit.commit_timezone = commit.author_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = msg
        repo.object_store.add_object(commit)
        repo.refs[b"HEAD"] = commit.id
        return commit.id

    prev = None
    for idx in range(1, 21):
        prev = _build_commit(
            repo,
            f"Fix {idx}.\n\nLink: https://lore.kernel.org/r/ft{idx}@test\n".encode(),
            parent=prev,
        )

    tree_name = "ft-stress"

    # --- set up WriterThread and read session ----------------------------
    writer = WriterThread.from_settings()
    writer.start()

    # A writable engine for cleanup; reads come from seeded_db sessions.
    engine = create_engine(settings.database_url, future=True)

    cache_durations: list[float] = []
    cache_errors: list[Exception] = []

    def _cache_set_fn(conn, key: str) -> None:
        # Mimic the shape of cache._direct_set: a single INSERT OR REPLACE
        # into the cache table.  expires_at is set to a far-future unix
        # timestamp so the row doesn't collide with real cache logic.
        conn.execute(
            text(
                "INSERT OR REPLACE INTO cache (key, value, expires_at) "
                "VALUES (:k, :v, :e)"
            ),
            {"k": key, "v": '{"x": 1}', "e": 9999999999},
        )

    def _submit_cache(i: int) -> None:
        key = f"phase3_ft_stress_{i}"
        t0 = time.perf_counter()
        try:
            writer.submit(
                WriteOp(
                    label=f"cache:set:{key}",
                    fn=functools.partial(_cache_set_fn, key=key),
                )
            ).result(timeout=10)
        except Exception as exc:
            cache_errors.append(exc)
        cache_durations.append(time.perf_counter() - t0)

    # --- drive the walk in a background thread ---------------------------
    walk_done = threading.Event()
    walk_exc: list[Exception] = []

    def _walk() -> None:
        try:
            with seeded_db() as session:
                walk_commits(
                    session,
                    repo_path,
                    tree_name=tree_name,
                    writer=writer,
                    batch_size=5,
                    branch="HEAD",
                )
        except Exception as exc:
            walk_exc.append(exc)
        finally:
            walk_done.set()

    walk_thread = threading.Thread(target=_walk, daemon=True)
    walk_thread.start()

    # --- fire 50 cache-set WriteOps from a thread pool -------------------
    # The pool submits each write as soon as a worker is free.  The 8
    # workers compete for the writer queue alongside the walk's batches.
    with ThreadPoolExecutor(max_workers=8) as pool_executor:
        for i in range(50):
            pool_executor.submit(_submit_cache, i)
    # All 50 .result() calls have returned by here (pool_executor.__exit__
    # joins all futures before leaving the block).

    walk_done.wait(timeout=60)

    # --- cleanup ---------------------------------------------------------
    # Remove the test rows regardless of assertion outcome so other tests
    # start with a clean state.
    try:
        with engine.connect() as c:
            c.execute(
                text("DELETE FROM mainline_commits WHERE tree_name = :t"),
                {"t": tree_name},
            )
            c.execute(
                text("DELETE FROM mainline_state WHERE tree_name = :t"),
                {"t": tree_name},
            )
            # Remove the synthetic cache rows inserted by the test.
            for i in range(50):
                c.execute(
                    text("DELETE FROM cache WHERE key = :k"),
                    {"k": f"phase3_ft_stress_{i}"},
                )
            c.commit()
    finally:
        engine.dispose()
        writer.stop(timeout=10)

    # --- assertions ------------------------------------------------------
    assert not walk_exc, f"walk_commits failed: {walk_exc[0]!r}"
    assert not cache_errors, f"cache WriteOps errored: {cache_errors}"
    assert cache_durations, "no cache durations collected"

    max_latency = max(cache_durations)
    assert max_latency < 5.0, (
        f"cache.set tail latency was {max_latency:.2f} s. Phase 3 "
        "promises bounded tails (well under the ~62 s pre-Phase-3 "
        "stall); a >5 s tail suggests the walker is holding the "
        "writer lock across batches instead of committing per batch."
    )
