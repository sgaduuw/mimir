"""Tests for the broker cache handlers (mimir.broker.handlers.cache):
the one-way cache_set submits to the active WriterThread and returns
no reply."""


def _wait_for_row(key, timeout=5.0):
    """Poll until the writer has committed `key`; the one-way handler
    returns before the commit, so the test has to wait for it."""
    import time

    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with SessionLocal() as session:
            if session.get(CacheEntry, key) is not None:
                return
        time.sleep(0.05)
    raise AssertionError(f"queued cache write for {key} never landed")


def test_cache_set_handler_commits_through_the_writer(seeded_db, broker_active):
    """The one-way handler returns no reply, and the write still lands
    through the active WriterThread."""
    from mimir.broker.handlers.cache import handle_cache_set
    from mimir.broker.protocol import CacheSetRequest

    key = "v3:one-way-contract"
    reply = handle_cache_set(
        CacheSetRequest(rpc_id=1, key=key, value_json='"x"', ttl=60)
    )
    assert reply is None
    _wait_for_row(key)


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
            assert handle_cache_set(req) is None
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
    # call submits to the active WriterThread (set up by the
    # broker_active fixture), the same path the cache worker thread
    # executes in production.
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


def test_cache_set_handler_returns_without_waiting_for_commit(broker_active):
    """#1157: a held writer (ANALYZE, a long WriteOp) must not stall
    the cache worker. handle_cache_set returns once the write is
    queued, and the value still lands when the writer frees."""
    import threading
    import time

    from mimir.broker import _context
    from mimir.broker.handlers.cache import handle_cache_set
    from mimir.broker.protocol import CacheSetRequest
    from mimir.broker.writes import WriteOp

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
        assert reply is None
        assert elapsed < 1.0, f"handler waited {elapsed:.1f}s on a held writer"
    finally:
        release.set()
        blocked_future.result(timeout=10)

    # The queued write commits once the writer is free.
    _wait_for_row(key)


def _saturate(writer):
    """Hold the writer on one op and fill its bounded queue behind it.
    Returns (release, futures); the caller sets `release` and resolves
    the futures when done."""
    import queue
    import threading

    from mimir.broker.writes import WriteOp

    release = threading.Event()
    started = threading.Event()

    def _hold(conn):
        started.set()
        release.wait(timeout=15)

    futures = [writer.submit(WriteOp(label="test:hold", fn=_hold))]
    # Only fill once the writer has dequeued the held op, else it frees
    # a slot behind us and the queue is not really full.
    assert started.wait(timeout=5)
    while True:
        try:
            futures.append(
                writer.submit(
                    WriteOp(label="test:fill", fn=lambda c: None), block=False
                )
            )
        except queue.Full:
            return release, futures


def test_cache_set_handler_drops_write_when_queue_full(broker_active, caplog):
    """#1157: a full writer queue must not block the cache worker. The
    write is dropped and the drop is logged with the key, the one place
    saturation shows up now that nobody receives a reply."""
    import logging
    import time

    from mimir.broker import _context
    from mimir.broker.handlers.cache import handle_cache_set
    from mimir.broker.protocol import CacheSetRequest

    release, futures = _saturate(_context.get_active_writer())
    try:
        key = "v3:full-queue-drop"
        start = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="mimir.broker.handlers.cache"):
            reply = handle_cache_set(
                CacheSetRequest(rpc_id=1, key=key, value_json='"x"', ttl=60)
            )
        assert time.monotonic() - start < 1.0
        assert reply is None
        assert f"cache_set dropped: writer queue full (key={key})" in caplog.text
    finally:
        release.set()
        for f in futures:
            f.result(timeout=10)


def test_cache_set_handler_does_not_wait_behind_blocked_producer(broker_active):
    """#1157: handle_cache_set must not block when another producer is
    already blocked on the full writer queue (submit used to hold
    _stop_lock across a blocking put)."""
    import threading
    import time

    from mimir.broker import _context
    from mimir.broker.handlers.cache import handle_cache_set
    from mimir.broker.protocol import CacheSetRequest
    from mimir.broker.writes import WriteOp

    writer = _context.get_active_writer()
    release, futures = _saturate(writer)
    producer = None
    try:
        # A blocking producer (stands in for a warm-run cache.set) parks
        # in put on the full queue.
        def _blocking_producer():
            futures.append(
                writer.submit(WriteOp(label="test:warm-like", fn=lambda c: None))
            )

        producer = threading.Thread(target=_blocking_producer, daemon=True)
        producer.start()
        time.sleep(0.2)
        assert producer.is_alive(), "producer should be parked on the full queue"

        result = {}

        def _call():
            result["reply"] = handle_cache_set(
                CacheSetRequest(
                    rpc_id=1, key="v3:behind-producer", value_json='"x"', ttl=60
                )
            )

        caller = threading.Thread(target=_call, daemon=True)
        caller.start()
        caller.join(timeout=2.0)
        assert not caller.is_alive(), (
            "handle_cache_set still blocked after 2 s behind a producer "
            "parked on the full writer queue"
        )
        assert result["reply"] is None
    finally:
        release.set()
        if producer is not None:
            producer.join(timeout=10)
        for f in list(futures):
            f.result(timeout=10)


def test_cache_set_handler_logs_commit_failure(broker_active, caplog):
    """#1157: handle_cache_set no longer reads the WriteFuture, so a
    failed commit must still leave a log line."""
    import logging
    import time
    from unittest.mock import patch

    from mimir.broker.handlers.cache import handle_cache_set
    from mimir.broker.protocol import CacheSetRequest
    from mimir.broker.writes import WriteOp

    submitted = []

    def _failing_set(w, nskey, value_json, ttl, *, block=True):
        def _boom(conn):
            raise RuntimeError("disk I/O error")

        f = w.submit(WriteOp(label=f"cache.set:{nskey}", fn=_boom), block=block)
        submitted.append(f)
        return f

    key = "v3:commit-fails"
    with caplog.at_level(logging.WARNING, logger="mimir.broker.handlers.cache"):
        with patch("mimir.cache._set_via_writer_for_nskey", _failing_set):
            reply = handle_cache_set(
                CacheSetRequest(rpc_id=1, key=key, value_json='"x"', ttl=60)
            )
        assert reply is None
        deadline = time.monotonic() + 5
        # The done callback runs on the writer thread just after the
        # future resolves, so poll the log, not the future.
        while (
            f"cache_set failed (key={key})" not in caplog.text
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert submitted[0].exception(timeout=1) is not None
        assert f"cache_set failed (key={key})" in caplog.text
