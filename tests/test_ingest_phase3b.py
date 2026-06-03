"""Phase 3b tests: per-batch composite WriteOp dispatch in ingest_epoch.

Pins the structural contract for Phase 3b of the broker two-pool
restructure (`_claude/specs/2026-05-29-broker-two-pool-design.md`).
Kept in its own file so the Phase 3b PR audit is easy.
"""

import pytest
from datetime import datetime, timezone

from mimir.ingest._pending import (
    _PendingWrites,
    _ArticleInsert,
    _ArticleListInsert,
    _ParseFailureRecord,
)


def test_pending_writes_initialises_empty():
    p = _PendingWrites(inbox_id=1, epoch="0.git")
    assert p.articles == []
    assert p.article_lists == []
    assert p.parse_failures == []
    assert p.address_observations == {}
    assert p.last_article_date_candidate is None
    assert p.last_commit_sha is None


def test_pending_writes_accumulates_records():
    p = _PendingWrites(inbox_id=1, epoch="0.git")
    p.articles.append(
        _ArticleInsert(
            message_id="phase3b-a@kernel.org",
            subject="t1",
            author="<hidden>",
            date=datetime(2026, 5, 30, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized="t1",
            canonical_inbox_id=None,
        )
    )
    p.article_lists.append(
        _ArticleListInsert(
            article_index=0,
            existing_article_id=None,
            inbox_id=1,
            epoch="0.git",
            commit_sha="a" * 40,
        )
    )
    p.parse_failures.append(
        _ParseFailureRecord(
            inbox_id=1,
            epoch="0.git",
            commit_sha="b" * 40,
            delete=False,
            error_class="ValueError",
            error_message="bad header",
        )
    )
    p.last_commit_sha = "a" * 40
    assert len(p.articles) == 1
    assert len(p.article_lists) == 1
    assert len(p.parse_failures) == 1
    assert p.last_commit_sha == "a" * 40


# ---------------------------------------------------------------------------
# _submit_ingest_batch tests (Phase 3b Task 4)
# ---------------------------------------------------------------------------


@pytest.fixture
def writer():
    """Function-scoped WriterThread for _submit_ingest_batch tests.

    Mirrors the `writer_thread` fixture in test_mainline.py: starts one
    WriterThread against the test DB and stops it after the test.
    `seeded_db` is not required as a parameter because the autouse
    `_reset_db` fixture already runs before this fixture is entered.
    """
    from mimir.broker.writes import WriterThread

    wt = WriterThread.from_settings()
    wt.start()
    yield wt
    wt.stop(timeout=10)


def test_submit_ingest_batch_inserts_articles_lists_and_advances_cursor(
    writer, seeded_db
):
    """Happy path: 2 new articles + 2 article_lists + cursor advance.

    Article ids are resolved via RETURNING and used as FKs in the
    matching ArticleList rows. The cursor (IngestState) lands LAST.
    """
    from mimir.extensions import SessionLocal
    from mimir.ingest._pending import _submit_ingest_batch
    from mimir.models import Article, ArticleList, IngestState

    # seeded_db seeds inbox `alpha` with id=1 (first inserted).
    # Grab the actual inbox id rather than hard-coding 1.
    with SessionLocal() as s:
        from mimir.models import Inbox

        inbox_id = s.execute(
            __import__("sqlalchemy").select(Inbox.id).where(Inbox.name == "alpha")
        ).scalar_one()

    pending = _PendingWrites(inbox_id=inbox_id, epoch="0.git")
    pending.articles.append(
        _ArticleInsert(
            message_id="phase3b-a@kernel.org",
            subject="t1",
            author="<hidden>",
            date=datetime(2026, 5, 30, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized="t1",
            canonical_inbox_id=None,
        )
    )
    pending.articles.append(
        _ArticleInsert(
            message_id="phase3b-b@kernel.org",
            subject="t2",
            author="<hidden>",
            date=datetime(2026, 5, 30, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized="t2",
            canonical_inbox_id=None,
        )
    )
    pending.article_lists.append(
        _ArticleListInsert(
            article_index=0,
            existing_article_id=None,
            inbox_id=inbox_id,
            epoch="0.git",
            commit_sha="a" * 40,
        )
    )
    pending.article_lists.append(
        _ArticleListInsert(
            article_index=1,
            existing_article_id=None,
            inbox_id=inbox_id,
            epoch="0.git",
            commit_sha="b" * 40,
        )
    )
    pending.last_commit_sha = "b" * 40

    future = _submit_ingest_batch(writer, pending)
    future.result(timeout=10)

    with SessionLocal() as s:
        import sqlalchemy as sa

        article_count = s.execute(
            sa.select(sa.func.count())
            .select_from(Article)
            .where(
                Article.message_id.in_(["phase3b-a@kernel.org", "phase3b-b@kernel.org"])
            )
        ).scalar_one()
        assert article_count == 2

        list_count = s.execute(
            sa.select(sa.func.count())
            .select_from(ArticleList)
            .join(Article, Article.id == ArticleList.article_id)
            .where(
                Article.message_id.in_(["phase3b-a@kernel.org", "phase3b-b@kernel.org"])
            )
        ).scalar_one()
        assert list_count == 2

        cursor = s.execute(
            sa.select(IngestState.last_commit_sha).where(
                IngestState.inbox_id == inbox_id,
                IngestState.epoch == "0.git",
            )
        ).scalar_one_or_none()
        assert cursor == "b" * 40


def test_submit_ingest_batch_existing_article_id_links_without_insert(
    writer, seeded_db
):
    """Cross-post case: article already exists; `_ArticleListInsert`
    carries `existing_article_id`; no new Article row is created, but
    the ArticleList row lands (in a second inbox) and the cursor advances.

    The seeded DB has art1 in alpha only. We submit it into beta via
    the existing_article_id path, asserting the Article count stays the
    same and the new ArticleList row appears under beta.
    """
    import sqlalchemy as sa

    from mimir.extensions import SessionLocal
    from mimir.ingest._pending import _submit_ingest_batch
    from mimir.models import Article, ArticleList, Inbox

    with SessionLocal() as s:
        # beta does NOT have art1; we'll cross-post it there.
        beta_id = s.execute(
            sa.select(Inbox.id).where(Inbox.name == "beta")
        ).scalar_one()
        existing_id = s.execute(
            sa.select(Article.id).where(Article.message_id == "art1@example.com")
        ).scalar_one()
        before_count = s.execute(
            sa.select(sa.func.count()).select_from(Article)
        ).scalar_one()

    pending = _PendingWrites(inbox_id=beta_id, epoch="0.git")
    # No new articles; article_list references an existing article_id.
    pending.article_lists.append(
        _ArticleListInsert(
            article_index=-1,
            existing_article_id=existing_id,
            inbox_id=beta_id,
            epoch="0.git",
            commit_sha="c" * 40,
        )
    )
    pending.last_commit_sha = "c" * 40

    future = _submit_ingest_batch(writer, pending)
    future.result(timeout=10)

    with SessionLocal() as s:
        after_count = s.execute(
            sa.select(sa.func.count()).select_from(Article)
        ).scalar_one()
        assert after_count == before_count, (
            "No new Article row should have been created"
        )

        # The ArticleList row should exist for (existing_id, beta_id).
        list_row = s.execute(
            sa.select(ArticleList).where(
                ArticleList.article_id == existing_id,
                ArticleList.inbox_id == beta_id,
            )
        ).scalar_one_or_none()
        assert list_row is not None
        assert list_row.commit_sha == "c" * 40


def test_submit_ingest_batch_empty_batch_is_noop(writer):
    """Empty batch returns a pre-resolved Future without submitting."""
    from mimir.ingest._pending import _submit_ingest_batch

    pending = _PendingWrites(inbox_id=1, epoch="0.git")
    future = _submit_ingest_batch(writer, pending)
    # Must already be done (pre-resolved) without ever waiting.
    assert future.done()
    assert future.result() is None


def test_submit_ingest_batch_parse_failure_delete_then_upsert(writer, seeded_db):
    """ParseFailure recovery: upsert a failure record, then submit a
    delete=True record for the same key; assert the row is gone.
    """
    import sqlalchemy as sa

    from mimir.extensions import SessionLocal
    from mimir.ingest._pending import _submit_ingest_batch
    from mimir.models import Inbox, ParseFailure

    with SessionLocal() as s:
        inbox_id = s.execute(
            sa.select(Inbox.id).where(Inbox.name == "alpha")
        ).scalar_one()

    # First: upsert a failure.
    pending_insert = _PendingWrites(inbox_id=inbox_id, epoch="0.git")
    pending_insert.parse_failures.append(
        _ParseFailureRecord(
            inbox_id=inbox_id,
            epoch="0.git",
            commit_sha="f" * 40,
            delete=False,
            error_class="ValueError",
            error_message="bad header",
            already_recorded=False,
        )
    )
    pending_insert.last_commit_sha = "f" * 40
    _submit_ingest_batch(writer, pending_insert).result(timeout=10)

    # Verify it landed.
    with SessionLocal() as s:
        row = s.get(ParseFailure, (inbox_id, "0.git", "f" * 40))
        assert row is not None
        assert row.error_class == "ValueError"
        assert row.attempts == 1

    # Now: submit a delete=True record for the same key.
    pending_delete = _PendingWrites(inbox_id=inbox_id, epoch="0.git")
    pending_delete.parse_failures.append(
        _ParseFailureRecord(
            inbox_id=inbox_id,
            epoch="0.git",
            commit_sha="f" * 40,
            delete=True,
        )
    )
    pending_delete.last_commit_sha = "f" * 40
    _submit_ingest_batch(writer, pending_delete).result(timeout=10)

    # Verify the row is gone.
    with SessionLocal() as s:
        row = s.get(ParseFailure, (inbox_id, "0.git", "f" * 40))
        assert row is None


def test_submit_ingest_batch_observation_upsert_batch(writer, seeded_db):
    """address_observations dict gets upserted into
    inbox_address_observations; an existing (inbox_id, address) row
    gets its count incremented and last_seen updated to the max.
    """
    import sqlalchemy as sa

    from mimir.extensions import SessionLocal
    from mimir.ingest._pending import _submit_ingest_batch
    from mimir.models import Inbox, InboxAddressObservation

    with SessionLocal() as s:
        inbox_id = s.execute(
            sa.select(Inbox.id).where(Inbox.name == "alpha")
        ).scalar_one()

    addr = "linux-kernel@vger.kernel.org"
    t1 = datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)

    # First batch: seed with count=3.
    pending1 = _PendingWrites(inbox_id=inbox_id, epoch="0.git")
    pending1.address_observations[addr] = (3, t1)
    pending1.last_commit_sha = "e" * 40
    _submit_ingest_batch(writer, pending1).result(timeout=10)

    with SessionLocal() as s:
        row = s.get(InboxAddressObservation, (inbox_id, addr))
        assert row is not None
        assert row.count == 3

    # Second batch: add 5 more; last_seen moves to t2.
    pending2 = _PendingWrites(inbox_id=inbox_id, epoch="0.git")
    pending2.address_observations[addr] = (5, t2)
    pending2.last_commit_sha = "e" * 40
    _submit_ingest_batch(writer, pending2).result(timeout=10)

    with SessionLocal() as s:
        row = s.get(InboxAddressObservation, (inbox_id, addr))
        assert row is not None
        assert row.count == 8  # additive: 3 + 5
        # last_seen should be t2 (the later timestamp).
        assert row.last_seen.replace(tzinfo=timezone.utc) >= t2


def test_submit_ingest_batch_last_article_date_conditional(writer, seeded_db):
    """last_article_date_candidate updates the Inbox row only when
    strictly greater than the current value; a lower or equal candidate
    is a no-op (the WHERE clause enforces this in one statement).
    """
    import sqlalchemy as sa

    from mimir.extensions import SessionLocal
    from mimir.ingest._pending import _submit_ingest_batch
    from mimir.models import Inbox

    with SessionLocal() as s:
        inbox_id = s.execute(
            sa.select(Inbox.id).where(Inbox.name == "alpha")
        ).scalar_one()
        # The seeded inbox has last_article_date = 2024-03-01.
        current_date = s.execute(
            sa.select(Inbox.last_article_date).where(Inbox.id == inbox_id)
        ).scalar_one()

    # Older candidate: should NOT update.
    older = datetime(2024, 1, 1, tzinfo=timezone.utc)
    pending_old = _PendingWrites(inbox_id=inbox_id, epoch="0.git")
    pending_old.last_article_date_candidate = older
    pending_old.last_commit_sha = "d1" + "0" * 38
    _submit_ingest_batch(writer, pending_old).result(timeout=10)

    with SessionLocal() as s:
        date_after_old = s.execute(
            sa.select(Inbox.last_article_date).where(Inbox.id == inbox_id)
        ).scalar_one()
    assert date_after_old == current_date, "Older candidate should not change the date"

    # Newer candidate: SHOULD update.
    newer = datetime(2026, 5, 30, tzinfo=timezone.utc)
    pending_new = _PendingWrites(inbox_id=inbox_id, epoch="0.git")
    pending_new.last_article_date_candidate = newer
    pending_new.last_commit_sha = "d2" + "0" * 38
    _submit_ingest_batch(writer, pending_new).result(timeout=10)

    with SessionLocal() as s:
        date_after_new = s.execute(
            sa.select(Inbox.last_article_date).where(Inbox.id == inbox_id)
        ).scalar_one()
    assert date_after_new is not None
    # Normalise to UTC for comparison (SQLite may return naive datetime).
    if date_after_new.tzinfo is None:
        date_after_new = date_after_new.replace(tzinfo=timezone.utc)
    assert date_after_new == newer


# ---------------------------------------------------------------------------
# _submit_promote_list_address and _submit_analyze tests (Phase 3b Task 5)
# ---------------------------------------------------------------------------


def test_submit_promote_list_address_runs_via_writer(writer, seeded_db):
    """The promote helper executes the same SQL semantics as the legacy
    _maybe_promote_list_address: observe address dominance threshold,
    promote list_address from NULL to the dominant address.

    Fixture: inbox with no list_address, seeded observations where one
    address covers >= 70% of the top-two combined. Submit the helper;
    assert the Inbox.list_address is set to the expected address.
    """
    import sqlalchemy as sa

    from mimir.extensions import SessionLocal
    from mimir.ingest._pending import _submit_promote_list_address
    from mimir.models import Inbox, InboxAddressObservation

    with SessionLocal() as s:
        inbox_id = s.execute(
            sa.select(Inbox.id).where(Inbox.name == "alpha")
        ).scalar_one()
        # Ensure list_address is NULL before the test.
        s.execute(
            sa.update(Inbox).where(Inbox.id == inbox_id).values(list_address=None)
        )
        s.commit()

    # Seed observations: one inbox with one dominant address (70+ count),
    # others far behind (15 and 5 count). Meets MIN_PROMOTE_OBSERVATIONS=50
    # and PROMOTE_DOMINANCE=0.7 (70 / (70+15) = 0.82).
    addr1 = "linux-kernel@vger.kernel.org"
    addr2 = "other@vger.kernel.org"
    addr3 = "minor@vger.kernel.org"

    with SessionLocal() as s:
        s.add_all(
            [
                InboxAddressObservation(
                    inbox_id=inbox_id,
                    address=addr1,
                    count=70,
                    last_seen=datetime(2026, 5, 30, tzinfo=timezone.utc),
                ),
                InboxAddressObservation(
                    inbox_id=inbox_id,
                    address=addr2,
                    count=15,
                    last_seen=datetime(2026, 5, 30, tzinfo=timezone.utc),
                ),
                InboxAddressObservation(
                    inbox_id=inbox_id,
                    address=addr3,
                    count=5,
                    last_seen=datetime(2026, 5, 30, tzinfo=timezone.utc),
                ),
            ]
        )
        s.commit()

    # Submit the helper; verify the promotion ran.
    future = _submit_promote_list_address(writer, inbox_id)
    future.result(timeout=10)

    with SessionLocal() as s:
        inbox = s.get(Inbox, inbox_id)
        assert inbox is not None
        assert inbox.list_address == addr1


def test_submit_analyze_runs_via_writer(writer, seeded_db):
    """The analyze helper runs ANALYZE on the writer's connection.

    ANALYZE's side effects are sqlite_stat1 internals, not directly
    observable, so the assertion is just "no exception raised".
    """
    from mimir.ingest._pending import _submit_analyze

    # Submit the helper with a valid inbox name.
    future = _submit_analyze(writer, "alpha")
    # Must complete without raising.
    future.result(timeout=30)


# ---------------------------------------------------------------------------
# Structural-contract test (Phase 3b Tasks 6+7)
# ---------------------------------------------------------------------------


def test_ingest_inbox_uses_writer_thread_via_active_context(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    """Phase 3b contract: `ingest_inbox` must NOT call `write_transaction`.

    The previous implementation held a write_transaction for the full
    epoch walk, blocking concurrent cache.set RPCs. Phase 3b restructures
    to dispatch all writes as WriteOps through the active WriterThread so
    the writer lock is held only for the brief per-batch commit, not the
    full epoch walk.

    This test monkeypatches `write_transaction` to raise RuntimeError and
    verifies that a normal `ingest_inbox` call succeeds without ever
    triggering the guard. Any call to the patched function indicates scope
    creep back to the old write-per-transaction model.
    """
    from sqlalchemy import select

    from mimir.models import Inbox
    from tests.test_ingest._helpers import (
        _build_pubinbox_repo,
        _rfc5322,
    )

    # Build a small mirror for alpha.
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _build_pubinbox_repo(
        mirror / "0.git",
        [_rfc5322(f"phase3b-contract-{i}@example.com") for i in range(3)],
    )
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.mirror_path = str(mirror)
        s.commit()
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()

    # Patch write_transaction to explode: any call is a contract violation.

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "write_transaction must not be called by ingest_inbox "
            "after Phase 3b (Tasks 6+7); all writes go through WriterThread"
        )

    # write_transaction is imported into orchestrate.py at module level.
    # After the Phase 3b rewrite it is no longer imported there, so we
    # patch mimir.extensions directly as well.
    import mimir.extensions as ext_mod

    monkeypatch.setattr(ext_mod, "write_transaction", _forbidden)

    from mimir.ingest import ingest_inbox

    results = ingest_inbox(inbox, workers=1)
    assert sum(r.new for r in results) == 3, (
        "All 3 messages should have been ingested as new articles"
    )


# ---------------------------------------------------------------------------
# Crash-replay idempotency (Phase 3b Task 8)
# ---------------------------------------------------------------------------


def test_mid_epoch_batch_failure_leaves_cursor_at_old_position(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    """Phase 3b cursor invariant: a crash between batch N and batch N+1
    leaves `IngestState.last_commit_sha` at batch N's last sha (not at
    batch N+1's, and not at NULL). The next tick re-walks from there;
    `INSERT OR IGNORE` on `articles.message_id` and
    `on_conflict_do_nothing` on `article_lists` make the replay a no-op
    for rows that did commit pre-crash.

    Mirrors Phase 3a's
    `test_mid_walk_cursor_failure_leaves_cursor_at_old_position`,
    adapted for the per-batch composite WriteOp shape: the injection
    point is the SECOND `_submit_ingest_batch` call (the first batch
    has already committed, including its terminal IngestState UPSERT,
    so the cursor is non-NULL but stale after the crash).
    """
    from concurrent.futures import Future

    from sqlalchemy import select

    import mimir.ingest._pending as pending_mod
    from mimir.config import settings
    from mimir.models import Article, ArticleList, IngestState, Inbox
    from tests.test_ingest._helpers import (
        _build_pubinbox_repo,
        _rfc5322,
    )

    # 4 messages → 4 batches when ingest_batch_flush_seconds=0 forces a
    # flush after every message. The 2nd batch's WriteOp fails; batches
    # 3+4 never run.
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _build_pubinbox_repo(
        mirror / "0.git",
        [_rfc5322(f"phase3b-crash-{i}@example.com") for i in range(4)],
    )
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.mirror_path = str(mirror)
        s.commit()
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()

    # Force per-message batches so we get >1 batch from 4 messages.
    monkeypatch.setattr(settings, "ingest_batch_flush_seconds", 0.0)

    real_submit = pending_mod._submit_ingest_batch
    submit_call_count = {"n": 0}
    first_batch_last_sha: dict[str, str | None] = {"sha": None}
    second_batch_attempted_sha: dict[str, str | None] = {"sha": None}

    def explode_on_second_call(writer, pending):
        submit_call_count["n"] += 1
        if submit_call_count["n"] == 2:
            # Capture what the cursor WOULD have advanced to if this
            # batch had committed. The inverse-shaped assertion below
            # verifies the on-disk cursor did NOT take this value.
            second_batch_attempted_sha["sha"] = pending.last_commit_sha
            f: Future = Future()
            f.set_exception(RuntimeError("simulated crash between batches"))
            return f
        # Record the first batch's cursor sha so the assertion below can
        # check that the post-crash cursor matches it (and not the second
        # batch's sha).
        if submit_call_count["n"] == 1:
            first_batch_last_sha["sha"] = pending.last_commit_sha
        return real_submit(writer, pending)

    monkeypatch.setattr(pending_mod, "_submit_ingest_batch", explode_on_second_call)

    from mimir.ingest import ingest_inbox

    # ingest_inbox propagates the RuntimeError up from the failed Future.
    with pytest.raises(RuntimeError, match="simulated crash"):
        ingest_inbox(inbox, workers=1)

    # State A: exactly the first batch's article(s) survived. Cursor is
    # at batch 1's last sha (the FINAL WriteOp in the composite for
    # batch 1 was the IngestState UPSERT), NOT at batch 2's sha and
    # NOT at None.
    with seeded_db() as s:
        articles_after_crash = (
            s.execute(
                select(Article.message_id)
                .where(Article.message_id.like("phase3b-crash-%"))
                .order_by(Article.id)
            )
            .scalars()
            .all()
        )
        cursor_after_crash = s.execute(
            select(IngestState.last_commit_sha).where(
                IngestState.inbox_id == inbox.id,
                IngestState.epoch == "0.git",
            )
        ).scalar_one_or_none()
        article_lists_after_crash = s.execute(
            select(ArticleList.article_id, ArticleList.commit_sha)
            .join(Article, Article.id == ArticleList.article_id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.message_id.like("phase3b-crash-%"),
            )
        ).all()

    assert len(articles_after_crash) >= 1, (
        "the first batch must have committed before the crash"
    )
    assert len(articles_after_crash) < 4, (
        "the crashing batch and everything after must NOT have committed"
    )
    assert cursor_after_crash is not None, (
        "cursor must have advanced past the first batch (its IngestState "
        "UPSERT lands as the FINAL step inside that batch's WriteOp)"
    )
    assert cursor_after_crash == first_batch_last_sha["sha"], (
        "cursor must be at the first batch's last sha, not the failed batch's"
    )
    # Inverse-shaped guard: the cursor must NOT have advanced to the
    # second (failed) batch's pending sha. Without this, a regression
    # that committed the cursor outside the batch's atomic closure (or
    # that wrote the cursor as the FIRST step of the closure instead of
    # the LAST) would also leave a cursor != first_batch_last_sha; the
    # original assertion would catch that, but a closely-related bug
    # where cursor = second_batch_last_sha would silently pass any
    # weaker "cursor advanced" check. Pinning both directions catches
    # the cursor-ordering invariant precisely (CONTEXT.md "Phase 3b":
    # the IngestState UPSERT is the FINAL statement of each batch's
    # composite WriteOp).
    assert second_batch_attempted_sha["sha"] is not None, (
        "test wiring: explode_on_second_call should have captured the "
        "second batch's pending.last_commit_sha before failing the Future"
    )
    assert second_batch_attempted_sha["sha"] != first_batch_last_sha["sha"], (
        "test wiring: batch sizing is wrong if batch 1 and batch 2 share "
        "the same last_commit_sha (would make the inverse assertion below "
        "tautologically true). With ingest_batch_flush_seconds=0 each "
        "message is its own batch, so the shas must differ."
    )
    assert cursor_after_crash != second_batch_attempted_sha["sha"], (
        "cursor must NOT have advanced to the failed batch's last sha. "
        "A regression that wrote the cursor BEFORE the batch's atomic "
        "boundary (or persisted it via a non-final WriteOp inside the "
        "closure) would let the cursor sit at the failed batch's value "
        "even though the data rows rolled back."
    )
    assert len(article_lists_after_crash) == len(articles_after_crash), (
        "one article_lists row per committed article"
    )

    # Replay: undo the monkeypatch and run again. on_conflict_do_nothing
    # on articles.message_id + article_lists' (article_id, inbox_id,
    # epoch, commit_sha) PK absorbs the duplicate-row attempts; the
    # remaining 3 batches commit cleanly; the cursor advances to HEAD.
    monkeypatch.undo()

    results = ingest_inbox(inbox, workers=1)

    with seeded_db() as s:
        articles_after_replay = (
            s.execute(
                select(Article.message_id)
                .where(Article.message_id.like("phase3b-crash-%"))
                .order_by(Article.id)
            )
            .scalars()
            .all()
        )
        cursor_after_replay = s.execute(
            select(IngestState.last_commit_sha).where(
                IngestState.inbox_id == inbox.id,
                IngestState.epoch == "0.git",
            )
        ).scalar_one()
        article_lists_after_replay = s.execute(
            select(ArticleList.article_id, ArticleList.commit_sha)
            .join(Article, Article.id == ArticleList.article_id)
            .where(
                ArticleList.inbox_id == inbox.id,
                Article.message_id.like("phase3b-crash-%"),
            )
        ).all()

    assert len(articles_after_replay) == 4, (
        f"expected exactly 4 articles after replay, got {len(articles_after_replay)}"
    )
    assert len(article_lists_after_replay) == 4, (
        f"expected exactly 4 article_lists rows after replay, "
        f"got {len(article_lists_after_replay)}"
    )
    assert cursor_after_replay != first_batch_last_sha["sha"], (
        "cursor should have advanced past the first batch on replay"
    )
    # Replay resumes from batch 1's cursor (commit 0's sha) so the walker
    # only yields commits 1..3 (three new articles); no dup_db tally,
    # because the cursor advance means commit 0 is never re-walked.
    total_new = sum(r.new for r in results)
    assert total_new == 3, (
        f"expected exactly 3 new articles on replay (commit 0 skipped via "
        f"cursor), got new={total_new}"
    )


# ---------------------------------------------------------------------------
# Free-threading stress (Phase 3b Task 9)
# ---------------------------------------------------------------------------


def test_cache_writes_drain_between_ingest_batches(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    """Phase 3b contract: while `ingest_inbox` dispatches batches
    through the writer, concurrent cache-set-shaped WriteOps drain
    between batches instead of head-of-line-stalling for the whole
    epoch walk.

    Pre-Phase 3b held one continuous `write_transaction("ingest_inbox:
    <name>")` for the entire walk. Phase 3b turns that into N short
    per-batch composite WriteOps, so cache-set WriteOps submitted
    from other threads see at most one batch worth of writer-lock
    hold, not the whole walk.

    Asserts: max per-cache-write wall time < 5 s (generous; on a
    healthy laptop the real numbers are tens of ms). The point is
    the absence of head-of-line stalls, not a wall-time speedup.

    Mirrors Phase 3a's
    `test_cache_writes_drain_between_mainline_batches` with the
    ingest_inbox loop replacing walk_commits as the writer-side
    driver.
    """
    import functools
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import select, text

    from mimir.broker._context import get_active_writer
    from mimir.broker.writes import WriteOp
    from mimir.config import settings
    from mimir.models import Inbox
    from tests.test_ingest._helpers import (
        _build_pubinbox_repo,
        _rfc5322,
    )

    # 20-message mirror with per-message flushes gives 20 batches; the
    # gaps between batches are where the concurrent cache writes drain.
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _build_pubinbox_repo(
        mirror / "0.git",
        [_rfc5322(f"phase3b-stress-{i}@example.com") for i in range(20)],
    )
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.mirror_path = str(mirror)
        s.commit()
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()

    monkeypatch.setattr(settings, "ingest_batch_flush_seconds", 0.0)

    writer = get_active_writer()
    cache_durations: list[float] = []
    cache_errors: list[Exception] = []

    def _cache_set_fn(conn, key: str) -> None:
        # Single INSERT OR REPLACE into the cache table. Mimics
        # cache._direct_set's shape so the WriteOp shares the same
        # writer-lock surface as a real cache.set.
        conn.execute(
            text(
                "INSERT OR REPLACE INTO cache (key, value, expires_at) "
                "VALUES (:k, :v, :e)"
            ),
            {"k": key, "v": '{"x": 1}', "e": 9999999999},
        )

    def _submit_cache(i: int) -> None:
        key = f"phase3b_stress_{i}"
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

    ingest_done = threading.Event()
    ingest_exc: list[Exception] = []

    def _ingest() -> None:
        try:
            from mimir.ingest import ingest_inbox

            ingest_inbox(inbox, workers=1)
        except Exception as exc:
            ingest_exc.append(exc)
        finally:
            ingest_done.set()

    ingest_thread = threading.Thread(target=_ingest, daemon=True)
    ingest_thread.start()

    # Fire 50 cache-set WriteOps across 8 threads; the pool blocks on
    # __exit__ until every .result() returned. They race ingest's batches
    # on the writer queue.
    with ThreadPoolExecutor(max_workers=8) as pool_executor:
        for i in range(50):
            pool_executor.submit(_submit_cache, i)

    ingest_done.wait(timeout=60)

    # Cleanup: remove the synthetic cache rows so they don't trip
    # subsequent tests' cache assertions.
    with seeded_db() as s:
        for i in range(50):
            s.execute(
                text("DELETE FROM cache WHERE key = :k"),
                {"k": f"phase3b_stress_{i}"},
            )
        s.commit()

    assert not ingest_exc, f"ingest_inbox failed: {ingest_exc[0]!r}"
    assert not cache_errors, f"cache WriteOps errored: {cache_errors}"
    assert cache_durations, "no cache durations collected"

    max_latency = max(cache_durations)
    assert max_latency < 5.0, (
        f"cache.set tail latency was {max_latency:.2f} s. Phase 3b "
        "promises bounded tails (well under the ~62 s pre-Phase-3b "
        "stall); a >5 s tail suggests ingest_inbox is holding the "
        "writer lock across batches instead of committing per batch."
    )


# ---------------------------------------------------------------------------
# Naive-datetime normalization on the ingest path (CONTEXT.md §9)
# ---------------------------------------------------------------------------


def test_ingest_handles_dash_zero_zero_zero_zero_date_alongside_aware(
    seeded_db, tmp_path, broker_active
):
    """A `-0000`-dated message must ingest cleanly when it lands in the
    same batch as a tz-aware message. RFC 5322 lets `-0000` mean "no
    time-zone information available"; CPython's `parsedate_to_datetime`
    returns a tz-NAIVE datetime for it, and `max(aware, naive)` raises
    TypeError, which used to roll back the whole ingest batch.

    The fix lives in `mimir.datetime_utils.aware_utc`, wired into
    `mimir/ingest/epoch.py` at the observation-tally `obs_time =
    aware_utc(parsed.date or commit_time)` call site (and at the
    matching site in `mimir/ingest/backfill.py`). CONTEXT.md §9
    documents the historical outage.

    This pins the regression: build two messages sharing a list address
    so observation tally accumulates across them (the exact code path
    where `max(prev_ts, obs_time)` blew up); give one a `-0000` Date
    header and the other a `+0200` Date header so without the wrap the
    second iteration's `max()` mixes aware + naive. Assert both land
    cleanly (`new == 2`, `failed == 0`).
    """
    from sqlalchemy import select

    from mimir.ingest import ingest_inbox
    from mimir.models import Inbox, InboxAddressObservation
    from tests.test_ingest._helpers import (
        _build_pubinbox_repo,
        _rfc5322_with_date,
    )

    mirror = tmp_path / "mirror"
    mirror.mkdir()
    # Same `To:` list address on both messages so observation tally
    # accumulates and hits the `max(prev_ts, obs_time)` codepath on
    # the second iteration. The kernel.org suffix matches LIST_HOST_SUFFIXES
    # so `extract_list_addresses` actually returns the address (a personal
    # @example.com would be filtered out and the codepath wouldn't fire).
    list_addr = "linux-kernel@vger.kernel.org"
    messages = [
        _rfc5322_with_date(
            "aware-tz@example.com",
            "Mon, 1 Jan 2024 12:00:00 +0200",
            to=list_addr,
        ),
        _rfc5322_with_date(
            "naive-dash-zero@example.com",
            "Mon, 1 Jan 2024 12:00:00 -0000",
            to=list_addr,
        ),
    ]
    _build_pubinbox_repo(mirror / "0.git", messages)

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.mirror_path = str(mirror)
        s.commit()
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()

    # Pre-Phase-3b, a naive-vs-aware comparison inside the batch
    # rolled the whole batch back. Post-fix, ingest completes cleanly
    # with both messages landed.
    results = ingest_inbox(inbox, workers=1)

    assert sum(r.new for r in results) == 2, (
        "both messages must ingest; if one batch rolled back due to a "
        "naive-vs-aware TypeError, `new` drops below 2"
    )
    assert sum(r.failed for r in results) == 0, (
        "no parse_failures expected; a TypeError in the batch closure "
        "would surface here"
    )

    # Inverse-shaped assertion: the observation row landed with an
    # aware UTC timestamp (not a naive one). A regression that dropped
    # the `aware_utc()` wrap would either roll back the batch (caught
    # above) OR land a naive `last_seen` in SQLite. SQLite strips tzinfo
    # on store, so the assertion below is "no exception thrown by the
    # comparator inside ingest_epoch", which the `new == 2` check above
    # already covers; here we just confirm the observation actually
    # accumulated to 2 across both messages.
    with seeded_db() as s:
        obs_count = s.execute(
            select(InboxAddressObservation.count).where(
                InboxAddressObservation.inbox_id == inbox.id,
                InboxAddressObservation.address == list_addr,
            )
        ).scalar_one()
    assert obs_count == 2, (
        f"observation tally must accumulate across both messages; got {obs_count}"
    )
