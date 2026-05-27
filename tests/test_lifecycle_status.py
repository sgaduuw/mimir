"""Per-state predicate coverage + bulk-query correctness for the
5-state lifecycle taxonomy (Landed / Superseded / Queued / Reviewed
/ Pending)."""

from datetime import datetime, timezone

from sqlalchemy import select

from mimir.lifecycle_status import (
    LifecycleStatus,
    lifecycle_status_for_articles,
)
from mimir.models import (
    Article,
    ArticleList,
    ArticleTrailer,
    Inbox,
    MainlineCommit,
)


def _seed_article(session, message_id, **overrides):
    """Minimal article seeded for lifecycle queries. Reuses the
    "lkml" inbox if it exists, creates it otherwise. The inbox
    values match what `validate_mirror_path` and
    `validate_upstream_url` accept (absolute path + HTTPS URL)."""
    inbox = session.execute(
        select(Inbox).where(Inbox.name == "lkml")
    ).scalar_one_or_none()
    if inbox is None:
        inbox = Inbox(
            name="lkml",
            mirror_path="/tmp/lkml",
            upstream_url="https://lore.kernel.org/lkml",
        )
        session.add(inbox)
        session.commit()
    art = Article(
        message_id=message_id,
        subject=overrides.get("subject", f"[PATCH] {message_id}"),
        date=overrides.get("date", datetime(2026, 4, 1, tzinfo=timezone.utc)),
        author=overrides.get("author", "Alice <a@x>"),
        patch_series_key=overrides.get("patch_series_key"),
        patch_series_position=overrides.get("patch_series_position"),
        patch_series_version=overrides.get("patch_series_version"),
    )
    session.add(art)
    session.commit()
    session.add(
        ArticleList(
            article_id=art.id,
            inbox_id=inbox.id,
            epoch="0.git",
            commit_sha="x" * 40,
        )
    )
    session.commit()
    return art


def test_landed_when_linus_row_exists(session):
    art = _seed_article(session, "landed@x")
    session.add(
        MainlineCommit(
            commit_sha="a" * 40,
            message_id="landed@x",
            tree_name="linus",
            committed_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
    )
    session.commit()
    got = lifecycle_status_for_articles(session, [art.id])
    assert got[art.id].state == LifecycleStatus.LANDED


def test_superseded_when_higher_version_exists(session):
    v1 = _seed_article(
        session,
        "v1@x",
        patch_series_key="series-a",
        patch_series_position=1,
        patch_series_version="1",
    )
    v2 = _seed_article(
        session,
        "v2@x",
        patch_series_key="series-a",
        patch_series_position=1,
        patch_series_version="2",
    )
    got = lifecycle_status_for_articles(session, [v1.id, v2.id])
    assert got[v1.id].state == LifecycleStatus.SUPERSEDED
    assert got[v2.id].state == LifecycleStatus.PENDING


def test_landed_outranks_superseded(session):
    """A landed v1 stays LANDED even when v2 exists."""
    v1 = _seed_article(
        session,
        "v1L@x",
        patch_series_key="series-b",
        patch_series_position=1,
        patch_series_version="1",
    )
    _seed_article(
        session,
        "v2L@x",
        patch_series_key="series-b",
        patch_series_position=1,
        patch_series_version="2",
    )
    session.add(
        MainlineCommit(
            commit_sha="b" * 40,
            message_id="v1L@x",
            tree_name="linus",
            committed_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
    )
    session.commit()
    got = lifecycle_status_for_articles(session, [v1.id])
    assert got[v1.id].state == LifecycleStatus.LANDED


def test_queued_when_only_non_linus_row(session):
    art = _seed_article(session, "queued@x")
    session.add(
        MainlineCommit(
            commit_sha="c" * 40,
            message_id="queued@x",
            tree_name="net-next",
            committed_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
        )
    )
    session.commit()
    got = lifecycle_status_for_articles(session, [art.id])
    assert got[art.id].state == LifecycleStatus.QUEUED
    assert got[art.id].tree == "net-next"


def test_reviewed_when_only_review_trailers(session):
    art = _seed_article(session, "reviewed@x")
    session.add(
        ArticleTrailer(
            article_id=art.id,
            role="Reviewed-by",
            name="Bob",
            address="b@x",
            address_normalized="b@x",
        )
    )
    session.commit()
    got = lifecycle_status_for_articles(session, [art.id])
    assert got[art.id].state == LifecycleStatus.REVIEWED


def test_signed_off_by_alone_does_not_count_as_reviewed(session):
    """Signed-off-by is authorship; only review-trailers
    (Reviewed-by, Acked-by, Tested-by) qualify for REVIEWED."""
    art = _seed_article(session, "sob@x")
    session.add(
        ArticleTrailer(
            article_id=art.id,
            role="Signed-off-by",
            name="Alice",
            address="a@x",
            address_normalized="a@x",
        )
    )
    session.commit()
    got = lifecycle_status_for_articles(session, [art.id])
    assert got[art.id].state == LifecycleStatus.PENDING


def test_pending_default(session):
    art = _seed_article(session, "pending@x")
    got = lifecycle_status_for_articles(session, [art.id])
    assert got[art.id].state == LifecycleStatus.PENDING


def test_bulk_mixed_states(session):
    a_landed = _seed_article(session, "ml1@x")
    session.add(
        MainlineCommit(
            commit_sha="d" * 40,
            message_id="ml1@x",
            tree_name="linus",
            committed_at=datetime.now(timezone.utc),
        )
    )
    a_queued = _seed_article(session, "mq1@x")
    session.add(
        MainlineCommit(
            commit_sha="e" * 40,
            message_id="mq1@x",
            tree_name="tip",
            committed_at=datetime.now(timezone.utc),
        )
    )
    a_pending = _seed_article(session, "mp1@x")
    session.commit()
    got = lifecycle_status_for_articles(
        session, [a_landed.id, a_queued.id, a_pending.id]
    )
    assert got[a_landed.id].state == LifecycleStatus.LANDED
    assert got[a_queued.id].state == LifecycleStatus.QUEUED
    assert got[a_pending.id].state == LifecycleStatus.PENDING


def test_empty_input_returns_empty(session):
    assert lifecycle_status_for_articles(session, []) == {}


def test_superseded_handles_double_digit_versions(session):
    """v10 supersedes v9 (regression: lexicographic comparison would
    have v10 NOT superseding v9 because "9" > "10" as strings)."""
    v9 = _seed_article(
        session,
        "v9@x",
        patch_series_key="series-z",
        patch_series_position=1,
        patch_series_version="9",
    )
    v10 = _seed_article(
        session,
        "v10@x",
        patch_series_key="series-z",
        patch_series_position=1,
        patch_series_version="10",
    )
    got = lifecycle_status_for_articles(session, [v9.id, v10.id])
    assert got[v9.id].state == LifecycleStatus.SUPERSEDED
    assert got[v10.id].state == LifecycleStatus.PENDING


def test_lifecycle_status_caches_per_article(session):
    from mimir import cache
    a = _seed_article(session, "cached@x")
    lifecycle_status_for_articles(session, [a.id])
    cached = cache.get(f"lifecycle_status:{a.id}")
    assert cached is not None


def test_lifecycle_status_uses_cache_for_hits(session, monkeypatch):
    a = _seed_article(session, "hit@x")
    lifecycle_status_for_articles(session, [a.id])
    call_count = {"n": 0}
    real_exec = session.execute
    def counting_exec(*args, **kwargs):
        call_count["n"] += 1
        return real_exec(*args, **kwargs)
    monkeypatch.setattr(session, "execute", counting_exec)
    lifecycle_status_for_articles(session, [a.id])
    assert call_count["n"] == 0, "cache hit should skip SQL"
