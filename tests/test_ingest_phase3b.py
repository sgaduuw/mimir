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
