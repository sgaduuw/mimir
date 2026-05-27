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


def test_lifecycle_status_info_carries_display_fields():
    """Per spec: LifecycleStatusInfo carries activity_heat,
    activity_detail, pill_label, count_suffix, tooltip with
    defaults for older cache rows."""
    from mimir.lifecycle_status import LifecycleStatusInfo
    info = LifecycleStatusInfo(state_value="pending")
    assert info.activity_heat == "dormant"
    assert info.activity_detail == "no replies"
    assert info.pill_label == ""
    assert info.count_suffix is None
    assert info.tooltip is None


def test_format_pill_label_per_state():
    from mimir.lifecycle_status import LifecycleStatus, _format_pill_label
    assert _format_pill_label(LifecycleStatus.LANDED, tree=None) == "LANDED"
    assert _format_pill_label(LifecycleStatus.QUEUED, tree="net-next") == "IN NET-NEXT"
    assert _format_pill_label(LifecycleStatus.QUEUED, tree="linux-next") == "IN LINUX-NEXT"
    assert _format_pill_label(LifecycleStatus.REVIEWED, tree=None) == "REVIEWED"
    assert _format_pill_label(LifecycleStatus.SUPERSEDED, tree=None) == "SUPERSEDED"


def test_format_count_suffix_omits_when_zero():
    from mimir.lifecycle_status import _format_count_suffix
    assert _format_count_suffix(0, 0) is None
    assert _format_count_suffix(1, 0) == ": 1 (0M)"
    assert _format_count_suffix(6, 3) == ": 6 (3M)"
    assert _format_count_suffix(12, 4) == ": 12 (4M)"


def test_format_tooltip_landed_with_reviewers():
    from mimir.lifecycle_status import (
        LifecycleStatus, _format_tooltip,
    )
    from datetime import datetime, timezone
    tip = _format_tooltip(
        state=LifecycleStatus.LANDED,
        linus_sha="feedface12345678",
        linus_committed_at=datetime(2025, 11, 22, 9, 30, tzinfo=timezone.utc),
        earliest_other_sha=None,
        earliest_other_at=None,
        superseded_by_version=None,
        superseded_by_posted_at=None,
        reviewers=[("Theodore Tso", True), ("Jan Kara", True)],
    )
    assert tip.startswith("feedface1234 · 2025-11-22 09:30")
    lines = tip.split("\n")
    assert lines[1] == "M Theodore Tso"
    assert lines[2] == "M Jan Kara"


def test_format_tooltip_queued_uses_earliest_other_tree():
    from mimir.lifecycle_status import LifecycleStatus, _format_tooltip
    from datetime import datetime, timezone
    tip = _format_tooltip(
        state=LifecycleStatus.QUEUED,
        linus_sha=None, linus_committed_at=None,
        earliest_other_sha="24be70885101abcd",
        earliest_other_at=datetime(2026, 5, 25, 14, 8, tzinfo=timezone.utc),
        superseded_by_version=None, superseded_by_posted_at=None,
        reviewers=[("Alice", True), ("Bob", False)],
    )
    assert tip.startswith("24be70885101 · 2026-05-25 14:08")
    assert "\nM Alice" in tip
    assert "\nBob" in tip


def test_format_tooltip_reviewed_names_only():
    from mimir.lifecycle_status import LifecycleStatus, _format_tooltip
    tip = _format_tooltip(
        state=LifecycleStatus.REVIEWED,
        linus_sha=None, linus_committed_at=None,
        earliest_other_sha=None, earliest_other_at=None,
        superseded_by_version=None, superseded_by_posted_at=None,
        reviewers=[("Alice", True), ("Bob", False), ("Carol", True)],
    )
    # Maintainers first (alphabetical via input order), then non-
    # maintainers. No leading commit line; no preamble.
    assert tip == "M Alice\nM Carol\nBob"


def test_format_tooltip_superseded_uses_supersede_note():
    from mimir.lifecycle_status import LifecycleStatus, _format_tooltip
    from datetime import datetime, timezone
    tip = _format_tooltip(
        state=LifecycleStatus.SUPERSEDED,
        linus_sha=None, linus_committed_at=None,
        earliest_other_sha=None, earliest_other_at=None,
        superseded_by_version="4",
        superseded_by_posted_at=datetime(2026, 5, 18, tzinfo=timezone.utc),
        reviewers=[("Alice", True), ("Bob", False)],
    )
    assert tip.startswith("Superseded by v4 posted 2026-05-18")
    assert "\nM Alice" in tip


def test_format_tooltip_landed_no_reviewers():
    from mimir.lifecycle_status import LifecycleStatus, _format_tooltip
    from datetime import datetime, timezone
    tip = _format_tooltip(
        state=LifecycleStatus.LANDED,
        linus_sha="cafebabe98765432",
        linus_committed_at=datetime(2026, 4, 14, 11, 8, tzinfo=timezone.utc),
        earliest_other_sha=None, earliest_other_at=None,
        superseded_by_version=None, superseded_by_posted_at=None,
        reviewers=[],
    )
    # Single line: just the commit info; no newline at the end.
    assert tip == "cafebabe9876 · 2026-04-14 11:08"


def test_format_tooltip_pending_returns_none():
    from mimir.lifecycle_status import LifecycleStatus, _format_tooltip
    tip = _format_tooltip(
        state=LifecycleStatus.PENDING,
        linus_sha=None, linus_committed_at=None,
        earliest_other_sha=None, earliest_other_at=None,
        superseded_by_version=None, superseded_by_posted_at=None,
        reviewers=[],
    )
    assert tip is None


def test_lifecycle_status_query_uses_index_seeks(session):
    """Pins the plan of the bulk lifecycle query: every LEFT JOIN
    source must SEARCH USING INDEX, not SCAN. Guards against an
    ANALYZE drift silently turning the listing-render hot path into
    a multi-row scan.

    Mirrors the shape of test_subsystem_path_filter_uses_index_seeks
    and test_triage_queries_use_date_index_no_full_scans.

    NOTE: SQLite's planner may SCAN tiny test-corpus tables (< ~100
    rows) regardless of indexes, because a full scan is genuinely
    cheaper at that cardinality. To make the pin meaningful on a
    small dataset the test seeds 50 articles, runs ANALYZE so the
    planner has stats to work with, then asserts the relaxed
    condition: at least one index SEARCH appears in the plan AND no
    load-bearing table is scanned. The per-table SCAN check is the
    regression guard that matters in production; the "at least one
    SEARCH" check ensures ANALYZE produced enough stats for the
    planner to use indexes at all.

    If this test becomes flaky on CI because 50 rows is too few for
    the planner to pick index paths, raise the seed count or relax
    the per-table assertion to apply only to tables with > 100 rows
    in the plan output.
    """
    from sqlalchemy import text

    from mimir.lifecycle_status import _BULK_SQL, _bulk_uncached

    # Seed enough rows that the planner has stats to work with.
    for i in range(50):
        _seed_article(session, f"plan{i}@x")
    session.execute(text("ANALYZE"))
    session.commit()

    # Force a non-empty IN clause similar to a real listing.
    ids = [r[0] for r in session.execute(text("SELECT id FROM articles LIMIT 20"))]
    _bulk_uncached(session, ids)

    # EXPLAIN QUERY PLAN against the compiled SQL. `_BULK_SQL` uses
    # SQLAlchemy's expanding bindparam, which `literal_binds` can't
    # inline for text() clauses. Instead, inline the id list directly
    # into the raw SQL string (safe: ids are ints from our own DB, not
    # user input) so EXPLAIN sees the real query shape.
    ids_literal = "(" + ", ".join(str(i) for i in ids) + ")"
    raw_sql = _BULK_SQL.text.replace(":ids", ids_literal)
    plan_rows = session.execute(text("EXPLAIN QUERY PLAN " + raw_sql)).all()
    plan_text = "\n".join(r[3] for r in plan_rows).lower()

    # No SCAN against the three load-bearing tables. On a 50-row
    # corpus these tables are tiny, but ANALYZE should still push the
    # planner toward index seeks on the message_id / article_id FKs.
    # If the planner legitimately picks a scan on such a small corpus,
    # this assertion fires; relax it to per-table row-count guards if
    # that happens in CI.
    for table in ("mainline_commits", "article_trailers", "articles"):
        assert f"scan {table}" not in plan_text, (
            f"query plan should not SCAN {table}; got:\n{plan_text}"
        )

    # At least one index SEARCH must appear (confirms ANALYZE ran and
    # the planner has stats).
    assert "search" in plan_text, (
        f"query plan has no index SEARCH at all; got:\n{plan_text}"
    )
