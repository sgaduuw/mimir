"""Tests for the broker cache handlers (mimir.broker.handlers.cache):
they dispatch through the active WriterThread."""


def test_cache_handlers_do_not_call_write_transaction(seeded_db, broker_active):
    """Phase 4 contract: the broker cache handlers dispatch via the
    active WriterThread, not via write_transaction. write_transaction
    was removed in Phase 6b; the behavioral assertions below pin
    that all four handlers complete successfully via the writer path.
    """
    from mimir.broker.handlers.cache import (
        handle_cache_delete,
        handle_cache_delete_for_inbox,
        handle_cache_purge_expired,
        handle_cache_set,
    )
    from mimir.broker.protocol import (
        CacheDeleteForInboxRequest,
        CacheDeleteRequest,
        CachePurgeExpiredRequest,
        CacheSetRequest,
    )

    # Each handler must complete without tripping the guard.
    r = handle_cache_set(
        CacheSetRequest(rpc_id=1, key="v3:phase4-contract", value_json='"x"', ttl=60)
    )
    assert r.ok is True

    r = handle_cache_delete(CacheDeleteRequest(rpc_id=1, key="v3:phase4-contract"))
    assert r.ok is True

    r = handle_cache_delete_for_inbox(
        CacheDeleteForInboxRequest(rpc_id=1, name="alpha")
    )
    assert r.ok is True

    r = handle_cache_purge_expired(CachePurgeExpiredRequest(rpc_id=1))
    assert r.ok is True


def test_cache_handlers_dispatch_under_long_op_load(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    """Phase 4 contract: while a long-op (backfill) is dispatching
    per-batch WriteOps through the writer, cache RPCs that arrive
    via the broker handler path complete with bounded latency.

    Pre-Phase-4 the cache handler wrote inline on the cache worker
    thread, parallel to the long worker's writes (serialised at
    SQLite's writer lock). Phase 4 routes cache
    writes through the same WriterThread as long-op writes, so
    they share one queue. Phase 3c's per-batch composite WriteOps
    have ~tens of ms per batch; cache RPCs should see at most one
    batch worth of wait.

    Asserts: max handle_cache_set latency < 5 s while a 20-article
    backfill_article_files runs in a background thread. Mirror of
    Phase 3c's test_cache_writes_drain_between_backfill_batches,
    but exercising the broker handler path instead of submitting
    WriteOps directly.
    """
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo
    from sqlalchemy import select, text

    from mimir.broker.handlers.cache import handle_cache_set
    from mimir.broker.protocol import CacheSetRequest
    from mimir.ingest import ingest_epoch
    from mimir.models import ArticleFile, Inbox

    _PATCH_BODY = (
        b"Signed-off-by: A <a@example>\n\n"
        b"diff --git a/fs/foo/a.c b/fs/foo/a.c\n"
        b"@@ -1 +1 @@\n-x\n+y\n"
    )

    def _rfc5322_stress(msgid: str) -> bytes:
        return (
            b"Message-ID: <" + msgid.encode() + b">\r\n"
            b"From: a@b.example\r\n"
            b"Subject: t\r\n"
            b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
            b"\r\n" + _PATCH_BODY
        )

    # Build a 20-article corpus and ingest, then drop ArticleFile rows
    # so backfill_article_files has work.
    repo_path = tmp_path / "0.git"
    repo = Repo.init_bare(str(repo_path), mkdir=True)
    parent: bytes | None = None
    for i in range(20):
        blob = Blob.from_string(_rfc5322_stress(f"phase4-stress-{i}@test"))
        repo.object_store.add_object(blob)
        tree = Tree()
        tree.add(b"m", 0o100644, blob.id)
        repo.object_store.add_object(tree)
        commit = Commit()
        commit.tree = tree.id
        commit.parents = [parent] if parent else []
        commit.author = commit.committer = b"test <t@x>"
        commit.commit_time = commit.author_time = 1700000000 + i
        commit.commit_timezone = commit.author_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = f"add {i}".encode()
        repo.object_store.add_object(commit)
        parent = commit.id
    repo.refs[b"HEAD"] = parent

    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        alpha.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, alpha, "0.git", repo_path, workers=1)
        s.query(ArticleFile).delete()
        s.commit()

    handler_durations: list[float] = []
    handler_errors: list[Exception] = []

    def _cache_handler_call(i: int) -> None:
        req = CacheSetRequest(
            rpc_id=1,
            key=f"v3:phase4_handler_stress_{i}",
            value_json='{"x": 1}',
            ttl=60,
        )
        t0 = time.perf_counter()
        try:
            r = handle_cache_set(req)
            assert r.ok is True
        except Exception as exc:
            handler_errors.append(exc)
        handler_durations.append(time.perf_counter() - t0)

    backfill_done = threading.Event()
    backfill_exc: list[Exception] = []

    def _backfill() -> None:
        try:
            from mimir.patches import backfill_article_files

            backfill_article_files()
        except Exception as exc:
            backfill_exc.append(exc)
        finally:
            backfill_done.set()

    backfill_thread = threading.Thread(target=_backfill, daemon=True)
    backfill_thread.start()

    # Fire 50 cache_set calls via the broker handler entry point. Each
    # call dispatches through the active WriterThread (set up by the
    # broker_active fixture) and awaits commit, the same path the
    # cache worker thread would execute in production.
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i in range(50):
            pool.submit(_cache_handler_call, i)

    backfill_done.wait(timeout=60)

    # Cleanup synthetic cache rows.
    with seeded_db() as s:
        for i in range(50):
            s.execute(
                text("DELETE FROM cache WHERE key = :k"),
                {"k": f"v3:phase4_handler_stress_{i}"},
            )
        s.commit()

    assert not backfill_exc, f"backfill failed: {backfill_exc[0]!r}"
    assert not handler_errors, f"handler errors: {handler_errors}"
    assert handler_durations, "no handler durations collected"

    max_latency = max(handler_durations)
    assert max_latency < 5.0, (
        f"cache handler tail latency was {max_latency:.2f} s. Phase 4 "
        "routes cache writes through the same WriterThread as long-op "
        "batches; a >5 s tail suggests a long-op batch is hogging the "
        "writer (Phase 3c per-batch contract regression)."
    )


def test_cache_handlers_swallow_timeout_error_during_writer_block(broker_active):
    """2.15.1 hotfix contract: when the writer is blocked (e.g. by VACUUM)
    and `.result(timeout=5)` raises `TimeoutError`, each cache handler
    catches it and returns `Reply(ok=False, error="writer busy")` with a
    WARNING log line instead of crashing loudly with a traceback.

    Pre-hotfix behaviour: dispatch's outer exception handler caught the
    TimeoutError and logged it as ERROR with full traceback per failure.
    During a 127-second VACUUM cycle on the production deploy, that
    produced ~30+ noisy traceback log entries before the queue drained.
    The fix matches the pre-Phase-4 best-effort posture: the client
    receives Reply(ok=False) and treats it as a transient cache miss
    (cached value still ages out via TTL).
    """
    import logging
    from concurrent.futures import Future
    from unittest.mock import patch

    from mimir.broker.handlers.cache import (
        handle_cache_delete,
        handle_cache_delete_for_inbox,
        handle_cache_purge_expired,
    )
    from mimir.broker.protocol import (
        CacheDeleteForInboxRequest,
        CacheDeleteRequest,
        CachePurgeExpiredRequest,
    )

    def _never_resolving(*args, **kwargs):
        # Return a Future pre-set with TimeoutError so .result(timeout=5)
        # raises immediately (rather than waiting 5s real time per call).
        # The handler catches TimeoutError regardless of whether the source
        # is the .result() wait timeout or the future's exception state.
        f: Future = Future()
        f.set_exception(TimeoutError())
        return f

    caplog_handler = logging.Handler()
    captured: list[logging.LogRecord] = []
    caplog_handler.emit = captured.append
    cache_handler_logger = logging.getLogger("mimir.broker.handlers.cache")
    cache_handler_logger.addHandler(caplog_handler)
    cache_handler_logger.setLevel(logging.WARNING)

    try:
        with patch("mimir.cache.delete_via_writer", _never_resolving):
            r = handle_cache_delete(CacheDeleteRequest(rpc_id=1, key="v3:hotfix-test"))
        assert r.ok is False
        assert r.error == "writer busy"

        with patch("mimir.cache.delete_for_inbox_via_writer", _never_resolving):
            r = handle_cache_delete_for_inbox(
                CacheDeleteForInboxRequest(rpc_id=1, name="alpha")
            )
        assert r.ok is False
        assert r.error == "writer busy"

        with patch("mimir.cache.purge_expired_via_writer", _never_resolving):
            r = handle_cache_purge_expired(CachePurgeExpiredRequest(rpc_id=1))
        assert r.ok is False
        assert r.error == "writer busy"

        # All three handlers logged at WARNING (not ERROR), one line each.
        assert len(captured) == 3
        for record in captured:
            assert record.levelno == logging.WARNING
            assert "writer busy" in record.getMessage()
    finally:
        cache_handler_logger.removeHandler(caplog_handler)


def test_cache_set_handler_replies_without_waiting_for_commit(broker_active):
    """#1157: a held writer (ANALYZE, a long WriteOp) must not stall
    the viewer. handle_cache_set replies once the write is queued, and
    the value still lands when the writer frees."""
    import threading
    import time

    from mimir.broker import _context
    from mimir.broker.handlers.cache import handle_cache_set
    from mimir.broker.protocol import CacheSetRequest
    from mimir.broker.writes import WriteOp
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    writer = _context.get_active_writer()
    release = threading.Event()
    # 15 s ceiling so a crashed test cannot hang the session.
    blocked_future = writer.submit(
        WriteOp(
            label="test:hold-writer",
            fn=lambda conn: release.wait(timeout=15),
        )
    )
    key = "v3:nonblocking-cache-set"
    try:
        start = time.monotonic()
        reply = handle_cache_set(
            CacheSetRequest(rpc_id=1, key=key, value_json='"x"', ttl=60)
        )
        elapsed = time.monotonic() - start
        assert reply.ok is True
        assert elapsed < 1.0, f"reply waited {elapsed:.1f}s on a held writer"
    finally:
        release.set()
        blocked_future.result(timeout=10)

    # The queued write commits once the writer is free.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with SessionLocal() as session:
            if session.get(CacheEntry, key) is not None:
                break
        time.sleep(0.05)
    else:
        raise AssertionError("queued cache write never landed")


def test_cache_set_handler_drops_write_when_queue_full(broker_active, caplog):
    """#1157: a full writer queue must not block the cache worker. The
    write is dropped, the reply is ok=False, and the drop is logged with
    the key so saturation shows up in prod logs."""
    import logging
    import queue
    import threading
    import time

    from mimir.broker import _context
    from mimir.broker.handlers.cache import handle_cache_set
    from mimir.broker.protocol import CacheSetRequest
    from mimir.broker.writes import WriteOp

    writer = _context.get_active_writer()
    release = threading.Event()
    started = threading.Event()

    def _hold(conn):
        started.set()
        release.wait(timeout=15)

    futures = [writer.submit(WriteOp(label="test:hold", fn=_hold))]
    try:
        # Only fill once the writer has dequeued the held op, else it
        # frees a slot behind us and the queue is not really full.
        assert started.wait(timeout=5)
        # Fill the bounded queue behind the held op.
        while True:
            try:
                futures.append(
                    writer.submit(
                        WriteOp(label="test:fill", fn=lambda c: None), block=False
                    )
                )
            except queue.Full:
                break
        key = "v3:full-queue-drop"
        start = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="mimir.broker.handlers.cache"):
            reply = handle_cache_set(
                CacheSetRequest(rpc_id=1, key=key, value_json='"x"', ttl=60)
            )
        assert time.monotonic() - start < 1.0
        assert reply.ok is False
        assert reply.error == "writer busy"
        assert f"cache_set dropped: writer queue full (key={key})" in caplog.text
    finally:
        release.set()
        for f in futures:
            f.result(timeout=10)
