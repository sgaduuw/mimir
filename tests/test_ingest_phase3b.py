"""Phase 3b tests: per-batch composite WriteOp dispatch in ingest_epoch.

Pins the structural contract for Phase 3b of the broker two-pool
restructure (`_claude/specs/2026-05-29-broker-two-pool-design.md`).
Kept in its own file so the Phase 3b PR audit is easy.
"""

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
