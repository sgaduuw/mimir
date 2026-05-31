"""Phase 4 tests: broker cache handlers migrate from inline
direct-SQLite writes (on the cache worker thread) to WriteOp
dispatch through the active WriterThread.

Pins the structural contract for Phase 4 of the broker two-pool
restructure (`_claude/specs/2026-05-29-broker-two-pool-design.md`).
Kept in its own file so the Phase 4 PR audit is easy.
"""

import pytest

from mimir.broker.writes import WriterThread


@pytest.fixture
def writer():
    """Function-scoped WriterThread for via-writer helper tests.

    Mirrors the `writer` fixture in tests/test_ingest_phase3c.py.
    The autouse `_reset_db` fixture seeds the DB before this fixture
    is entered, so writes against the seeded `cache` rows are safe.
    """
    wt = WriterThread.from_settings()
    wt.start()
    yield wt
    wt.stop(timeout=10)


def test_set_via_writer_for_nskey_inserts_row(writer, seeded_db):
    """`_set_via_writer_for_nskey` upserts a pre-namespaced row with
    pre-encoded JSON payload. Mirror of `_direct_set`'s signature."""
    from sqlalchemy import select

    from mimir.cache import _now, _set_via_writer_for_nskey
    from mimir.models import CacheEntry

    nskey = "v3:phase4-set-test"
    payload = '{"x": 1}'
    _set_via_writer_for_nskey(writer, nskey, payload, ttl=60).result(timeout=10)

    with seeded_db() as s:
        row = s.execute(
            select(CacheEntry.value, CacheEntry.expires_at).where(
                CacheEntry.key == nskey
            )
        ).one()
    assert row.value == payload
    # expires_at is now+ttl; allow generous wiggle for test slowness.
    assert row.expires_at >= _now() + 30


def test_set_via_writer_for_nskey_upserts_on_conflict(writer, seeded_db):
    """Repeat call on the same nskey UPDATEs value + expires_at."""
    from sqlalchemy import select

    from mimir.cache import _set_via_writer_for_nskey
    from mimir.models import CacheEntry

    nskey = "v3:phase4-set-conflict"
    _set_via_writer_for_nskey(writer, nskey, '"old"', ttl=60).result(timeout=10)
    _set_via_writer_for_nskey(writer, nskey, '"new"', ttl=120).result(timeout=10)

    with seeded_db() as s:
        value = s.execute(
            select(CacheEntry.value).where(CacheEntry.key == nskey)
        ).scalar_one()
    assert value == '"new"'


def test_delete_via_writer_removes_row_and_returns_rowcount(writer, seeded_db):
    """`delete_via_writer` removes the namespaced row and returns 1
    via the WriteFuture. Repeat call returns 0 (idempotent)."""
    from sqlalchemy import insert, select

    from mimir.cache import delete_via_writer
    from mimir.models import CacheEntry

    nskey = "v3:phase4-delete-test"
    with seeded_db() as s:
        s.execute(
            insert(CacheEntry).values(key=nskey, value="x", expires_at=9999999999)
        )
        s.commit()

    n = delete_via_writer(writer, nskey).result(timeout=10)
    assert n == 1

    with seeded_db() as s:
        present = s.execute(
            select(CacheEntry.key).where(CacheEntry.key == nskey)
        ).one_or_none()
    assert present is None

    n2 = delete_via_writer(writer, nskey).result(timeout=10)
    assert n2 == 0


def test_delete_for_inbox_via_writer_removes_matching_rows(writer, seeded_db):
    """`delete_for_inbox_via_writer` removes every cache row whose key
    references the named inbox (suffix `:{name}` or middle `:{name}:`).
    Returns the rowcount via the WriteFuture."""
    from sqlalchemy import insert, select

    from mimir.cache import delete_for_inbox_via_writer
    from mimir.models import CacheEntry

    with seeded_db() as s:
        s.execute(
            insert(CacheEntry).values(
                [
                    {
                        "key": "v3:foo:phase4inbox",
                        "value": "1",
                        "expires_at": 9999999999,
                    },
                    {
                        "key": "v3:bar:phase4inbox:more",
                        "value": "2",
                        "expires_at": 9999999999,
                    },
                    {"key": "v3:baz:other", "value": "3", "expires_at": 9999999999},
                ]
            )
        )
        s.commit()

    n = delete_for_inbox_via_writer(writer, "phase4inbox").result(timeout=10)
    assert n == 2

    with seeded_db() as s:
        remaining = (
            s.execute(
                select(CacheEntry.key).where(
                    CacheEntry.key.in_(
                        [
                            "v3:foo:phase4inbox",
                            "v3:bar:phase4inbox:more",
                            "v3:baz:other",
                        ]
                    )
                )
            )
            .scalars()
            .all()
        )
    assert "v3:baz:other" in remaining
    assert "v3:foo:phase4inbox" not in remaining
    assert "v3:bar:phase4inbox:more" not in remaining


def test_purge_expired_via_writer_drops_expired_only(writer, seeded_db):
    """`purge_expired_via_writer` removes rows whose
    `expires_at < cutoff` (captured at submit time) and returns the
    rowcount. Non-expired rows are untouched."""
    from sqlalchemy import insert, select

    from mimir.cache import _now, purge_expired_via_writer
    from mimir.models import CacheEntry

    now = _now()
    with seeded_db() as s:
        s.execute(
            insert(CacheEntry).values(
                [
                    {"key": "v3:phase4-stale-1", "value": "a", "expires_at": now - 10},
                    {"key": "v3:phase4-stale-2", "value": "b", "expires_at": now - 1},
                    {"key": "v3:phase4-fresh", "value": "c", "expires_at": now + 3600},
                ]
            )
        )
        s.commit()

    n = purge_expired_via_writer(writer).result(timeout=10)
    # Other expired rows from the test session may exist; assert only
    # that the two staged stale rows were among the deletions.
    assert n >= 2

    with seeded_db() as s:
        fresh = s.execute(
            select(CacheEntry.key).where(CacheEntry.key == "v3:phase4-fresh")
        ).one_or_none()
        stale_1 = s.execute(
            select(CacheEntry.key).where(CacheEntry.key == "v3:phase4-stale-1")
        ).one_or_none()
        stale_2 = s.execute(
            select(CacheEntry.key).where(CacheEntry.key == "v3:phase4-stale-2")
        ).one_or_none()
    assert fresh is not None
    assert stale_1 is None
    assert stale_2 is None


def test_cache_handlers_do_not_call_write_transaction(
    seeded_db, monkeypatch, broker_active
):
    """Phase 4 contract: the broker cache handlers must NOT call
    `write_transaction`. Pre-4 they wrapped `_direct_*` in
    `write_transaction("broker:cache_*")`; Phase 4 dispatches via
    the active WriterThread instead. Monkeypatching `write_transaction`
    to raise asserts none of the four cache handlers reach it.
    """
    import mimir.extensions as ext_mod

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "write_transaction must not be called by Phase 4 cache handlers"
        )

    monkeypatch.setattr(ext_mod, "write_transaction", _forbidden)

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
        CacheSetRequest(key="v3:phase4-contract", value_json='"x"', ttl=60)
    )
    assert r.ok is True

    r = handle_cache_delete(CacheDeleteRequest(key="v3:phase4-contract"))
    assert r.ok is True

    r = handle_cache_delete_for_inbox(CacheDeleteForInboxRequest(name="alpha"))
    assert r.ok is True

    r = handle_cache_purge_expired(CachePurgeExpiredRequest())
    assert r.ok is True


def test_cache_handlers_dispatch_under_long_op_load(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    """Phase 4 contract: while a long-op (backfill) is dispatching
    per-batch WriteOps through the writer, cache RPCs that arrive
    via the broker handler path complete with bounded latency.

    Pre-Phase-4 the cache handler ran _direct_set inline on the
    cache worker thread, parallel to the long worker's writes
    (serialised at SQLite's writer lock). Phase 4 routes cache
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
