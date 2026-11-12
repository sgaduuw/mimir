"""Grouped recent postings keep their limit independent of posting size."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from mimir.models import Article, Inbox
from mimir.subsystems_dashboard.recent import recent_postings_query
from tests.test_subsystems._helpers import _add_patch_article, _add_subsystem


def test_group_before_limit_and_bound_matching_preview(seeded_db):
    with seeded_db() as session:
        sub = _add_subsystem(session, "GROUPS", "Maintained", ["drivers/demo/"])
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        now = datetime.now(UTC)
        for position in range(1, 36):
            aid = _add_patch_article(
                session, f"series-{position}@x", ["drivers/demo/a.c"]
            )
            article = session.get(Article, aid)
            article.subject = f"[PATCH  {position}/35] Change {position}"
            article.patch_series_key = "series"
            article.patch_series_version = "v1"
            article.patch_series_position = position
            article.date = now
        for index in range(29):
            aid = _add_patch_article(session, f"solo-{index}@x", ["drivers/demo/a.c"])
            article = session.get(Article, aid)
            article.subject = f"[PATCH] Solo {index}"
            article.date = now - timedelta(days=1)
        session.commit()
        rows = recent_postings_query(session, inbox, sub, 30, 30)
        assert len(rows) == 30
        posting = rows[0]
        assert posting.matching_count == 35
        assert len(posting.children) == 30
        assert [p.position for p in posting.children] == list(range(1, 31))
        assert posting.cover is None
        assert all(not p.children for p in rows[1:])


def test_hostile_numeric_subject_cannot_abort_dashboard():
    from time import monotonic

    from mimir.subsystems_dashboard.recent import _canonical_patch

    start = monotonic()
    assert not _canonical_patch("[RFC " + "9" * 5000 + "/6] title", None, None)
    assert monotonic() - start < 2


def _patch(
    session,
    mid,
    *,
    key="shared",
    head=None,
    version="v1",
    position=1,
    paths=("drivers/demo/a.c",),
    inbox="alpha",
    subject=None,
):
    aid = _add_patch_article(session, mid, paths, inbox_name=inbox)
    article = session.get(Article, aid)
    article.subject = subject or f"[PATCH {position}/40] {mid}"
    article.patch_series_key = key
    article.patch_series_head_id = head
    article.patch_series_version = version
    article.patch_series_position = position
    session.flush()
    return article


@pytest.mark.parametrize("limit", [5, 30], ids=["recent-window", "history-fallback"])
def test_exact_identity_slot_winners_and_canonical_patch_filter(seeded_db, limit):
    with seeded_db() as session:
        sub = _add_subsystem(
            session, "IDENTITY", None, ["drivers/demo/"], ["drivers/demo/excluded/"]
        )
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        bare = _patch(session, "bare@x", paths=())
        winner = _patch(session, "winner@x")
        _patch(session, "resend@x")
        excluded = _patch(
            session, "excluded@x", position=2, paths=("drivers/demo/excluded/a.c",)
        )
        _patch(session, "eligible-copy@x", position=2)
        _patch(session, "elsewhere@x", position=3, inbox="beta")
        _patch(session, "v2@x", version="v2")
        _patch(session, "rfc@x", version="rfc")
        _patch(session, "new-head@x", head=winner.id)
        _patch(
            session,
            "orphan@x",
            key=None,
            position=None,
            version=None,
            subject="[PATCH 1/2] Orphan reply",
        )
        _patch(session, "reply@x", subject="Re: [PATCH 1/40] reply")
        _patch(session, "prose@x", key=None, position=None, subject="Discussion")
        _patch(session, "incomplete@x", version=None)
        # Both branches see the same recent identity fixture. Five groups fill
        # the bounded request; a limit of thirty must use the history fallback.
        now = datetime.now(UTC)
        for article in session.scalars(select(Article)).all():
            article.date = now
        session.commit()
        assert winner.date == now
        rows = recent_postings_query(session, inbox, sub, limit=limit)
        assert len(rows) == 5  # v1, v2, RFC, a distinct head, and singleton fallback
        first = next(p for p in rows if len(p.children) == 2)
        assert first.matching_count == 2
        assert [p.message_id for p in first.children] == ["winner@x", "eligible-copy@x"]
        assert bare.id < winner.id < excluded.id
        assert first.status_id is None
        assert {p.version for p in rows} == {"v1", "v2", "rfc", None}


def test_cover_full_posting_rollup_is_independent_of_preview(seeded_db):
    from mimir.lifecycle_status import lifecycle_status_for_articles
    from mimir.models import ArticleTrailer

    with seeded_db() as session:
        sub = _add_subsystem(session, "ROLLUP", None, ["drivers/demo/"])
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        bare_cover = _patch(
            session,
            "cover-bare@x",
            position=0,
            paths=(),
            subject="[PATCH 0/40] Whole posting",
        )
        cover = _patch(
            session,
            "cover-diff@x",
            position=0,
            inbox="beta",
            paths=("elsewhere/a.c",),
            subject="[PATCH 0/40] Whole posting",
        )
        for position in range(1, 39):
            article = _patch(
                session,
                f"rollup-{position}@x",
                position=position,
                inbox="beta" if position == 38 else "alpha",
                paths=("drivers/demo/a.c",) if position <= 35 else ("elsewhere/a.c",),
            )
            session.add(
                ArticleTrailer(
                    article_id=article.id,
                    role="Reviewed-by",
                    name="Reviewer",
                    address="r@x",
                    address_normalized="r@x",
                )
            )
        session.commit()
        rows = recent_postings_query(session, inbox, sub)
        assert len(rows) == 1
        row = rows[0]
        assert bare_cover.id < cover.id
        assert row.cover.article_id == cover.id
        assert row.cover.inbox_name == "beta"
        assert row.title == "Whole posting"
        assert row.matching_count == 35 and len(row.children) == 30
        statuses = lifecycle_status_for_articles(session, [row.status_id], inbox.id)
        assert statuses[row.status_id].rollup == "38 of 40 reviewed"
        assert statuses[row.status_id].state_value == "pending"


def test_query_statement_count_and_selected_cover_plan(seeded_db):
    from sqlalchemy import event

    from mimir.models import Subsystem
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    with seeded_db() as session:
        sub = _add_subsystem(session, "STATEMENTS", None, ["drivers/demo/"])
        sid = sub.id
        for index in range(12):
            _patch(session, f"statement-{index}@x", key=f"key-{index}")
        session.commit()
    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        sub = session.get(Subsystem, sid)
        list(sub.paths)
        statements = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            rows = recent_postings_query(session, inbox, sub)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert len(rows) == 12
        assert len(statements) == 5  # One bounded attempt, then the full-history path.
        plan = plan_of(
            session,
            lambda: recent_postings_query(session, inbox, sub),
            "SELECT g.id AS representative_id, c.id",
        )
        assert "ix_articles_patch_series_key" in plan
        assert "SCAN p" not in plan


def test_primitive_cache_roundtrip_separation_and_inbox_invalidation(seeded_db):
    from mimir import cache
    from mimir.subsystems_dashboard.recent import (
        pack_postings,
        recent_postings_in_subsystem,
        unpack_postings,
    )

    with seeded_db() as session:
        sub = _add_subsystem(session, "CACHE", None, ["drivers/demo/"])
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        _patch(session, "cached@x")
        session.commit()
        cache.set(f"recent_articles_in_subsystem:alpha:{sub.id}:30", ["old schema"], 60)
        rows = recent_postings_in_subsystem(session, inbox, sub)
        assert rows == unpack_postings(pack_postings(rows))
        key = f"recent_postings_v1:alpha:{sub.id}:30:30"
        assert cache.get(key) == pack_postings(rows)
        assert cache.get(f"recent_postings_v1:alpha:{sub.id}:30:5") is None
        cache.delete_for_inbox("alpha")
        assert cache.get(key) is None


def test_route_and_slow_warm_use_same_cache_and_bounds(client, seeded_db, monkeypatch):
    import pytest

    from mimir import cache
    from mimir.cli.cache import _per_subsystem_warm_call
    from mimir.subsystems_dashboard.recent import (
        recent_postings_in_subsystem,
        warm_postings,
    )

    with seeded_db() as session:
        sub = _add_subsystem(session, "WARM", None, ["drivers/demo/"])
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        _patch(session, "warm@x")
        session.commit()
        labels = _per_subsystem_warm_call(session, inbox, sub)
        assert "alpha/WARM recent_postings_in_subsystem" in labels
        key = f"recent_postings_v1:alpha:{sub.id}:30:30"
        assert cache.get(key) is not None

        # A real warm must satisfy the route without recomputing the grouped read.
        def unexpected(*args, **kwargs):
            raise AssertionError("grouped query ran after its slow warm")

        monkeypatch.setattr(
            "mimir.subsystems_dashboard.recent.recent_postings_query", unexpected
        )
        response = client.get("/alpha/subsystem/warm/")
        assert response.status_code == 200
        assert "Posting without cover" in response.get_data(as_text=True)
        rows = recent_postings_in_subsystem(session, inbox, sub)
        monkeypatch.setattr(cache, "MAX_CACHE_VALUE_BYTES", 1)
        with pytest.raises(ValueError, match="cache value limit"):
            warm_postings(rows)
        with pytest.raises(ValueError, match="cache value limit"):
            _per_subsystem_warm_call(session, inbox, sub)


def test_dashboard_disclosures_source_counts_and_unusable_links(
    client, seeded_db, monkeypatch
):
    import re

    from mimir.models import ArticleTrailer, SubsystemMaintainer
    from mimir.subsystems_dashboard.triage import PatchAttention

    with seeded_db() as session:
        sub = _add_subsystem(
            session,
            "DISPLAY",
            "Maintained",
            ["drivers/demo/"],
            ["drivers/demo/excluded/"],
        )
        sub.maintainers.append(
            SubsystemMaintainer(role="M", name="Safe Name", address="bad\n@kernel.org")
        )
        sub.maintainers.append(
            SubsystemMaintainer(
                role="R", name="private@example.net", address="r@example.net"
            )
        )
        cover = _patch(
            session,
            "display-cover@x",
            position=0,
            paths=(),
            subject="[PATCH 0/35] <script>Cover</script>",
        )
        cover.date = None
        for position in range(1, 36):
            child = _patch(session, f"display-{position}@x", position=position)
            if position == 1:
                child.date = None
        reviewed = _patch(
            session,
            "reviewed@x",
            key=None,
            version=None,
            position=None,
            subject="[PATCH] reviewed",
        )
        for index in range(4):
            session.add(
                ArticleTrailer(
                    article_id=reviewed.id,
                    role="Reviewed-by",
                    name="Same Person",
                    address="r@example.net",
                    address_normalized="r@example.net",
                )
            )
        session.commit()
        triage = [
            PatchAttention(
                article_id=reviewed.id,
                message_id=reviewed.message_id,
                subject=f"[PATCH] Triage {i}",
                author="Safe Author <private@example.net>",
                date=reviewed.date,
                inbox_name="alpha",
                series_more=2,
            )
            for i in range(10)
        ]
    monkeypatch.setattr(
        "mimir.web.routes.dashboards.needs_attention_patches_in_subsystem",
        lambda *a, **kw: triage,
    )
    html = client.get("/alpha/subsystem/display/").get_data(as_text=True)
    triage_html = re.search(
        r'<section class="subsystem-triage".*?</section>', html, re.DOTALL
    ).group()
    before, after = triage_html.split('<details class="subsystem-more">')
    assert 'id="triage-heading">Reviewed, not picked up' in before
    assert before.count("<li>") == 5
    assert after.count("<li>") == 5
    assert "<summary>Show all 10 (5 more)</summary>" in after
    assert "4 reviews, 4 by maintainers" in html
    assert "review trailers," not in triage_html
    assert "Showing first 30 of 35 matching patches" in html
    children = re.search(
        r'<ul class="subsystem-children">(.*?)</ul>', html, re.DOTALL
    ).group(1)
    assert children.count("<li>") == 30
    assert '<details class="subsystem-quiet"><summary>' in html
    assert "Safe Name" in html and "Unnamed reviewer" in html
    assert "private@example.net" not in html
    assert "<script>Cover</script>" not in html
    assert not any("/0000/00/" in link for link in re.findall(r'href="([^"]*)"', html))
    assert "Entire posting" in html and "outside this subsystem and inbox" in html
    files = re.search(r'<ul class="subsystem-files">(.*?)</ul>', html, re.DOTALL).group(
        1
    )
    assert files.count("drivers/demo/") == 2


def test_only_inbox_matching_candidates_reach_subject_parser(seeded_db, monkeypatch):
    from mimir.models import Subsystem
    from mimir.subsystems_dashboard import recent
    from tests._helpers import PRODUCTION_STATS, planner_stats

    with seeded_db() as session:
        sub = _add_subsystem(session, "PARSE SCOPE", None, ["drivers/demo/"])
        sid = sub.id
        first = _patch(session, "first@x")
        _patch(session, "resend@x")
        _patch(session, "v2@x", version="v2")
        _patch(session, "other-head@x", head=first.id)
        for index in range(20):
            _patch(session, f"other-inbox-{index}@x", inbox="beta")
        session.commit()
    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as session:
        sub = session.get(Subsystem, sid)
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        parser = recent._canonical_patch
        parsed = []

        def observed(subject, position, key):
            parsed.append(subject)
            return parser(subject, position, key)

        monkeypatch.setattr(recent, "_canonical_patch", observed)
        rows = recent_postings_query(session, inbox, sub)
        assert len(rows) == 3
        assert sorted(p.matching_count for p in rows) == [1, 1, 1]
        assert {p.children[0].message_id for p in rows} == {
            "first@x",
            "v2@x",
            "other-head@x",
        }
        assert len(parsed) == 4
        assert all("other-inbox" not in subject for subject in parsed)


def test_recent_selection_probes_full_posting_members_by_series(seeded_db):
    from mimir.models import Subsystem
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    with seeded_db() as session:
        sub = _add_subsystem(session, "RECENT WINDOW", None, ["drivers/demo/"])
        sid = sub.id
        original = _patch(session, "original@x")
        original.date = datetime.now(UTC) - timedelta(days=60)
        undated = _patch(session, "undated@x", position=2)
        undated.date = None
        resend = _patch(session, "recent-resend@x")
        resend.date = datetime.now(UTC)
        _patch(session, "other-inbox@x", position=3, inbox="beta")
        session.commit()
        assert original.id < resend.id
        assert resend.date > datetime.now(UTC) - timedelta(hours=1)
        assert undated.date is None
    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as session:
        sub = session.get(Subsystem, sid)
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        results = []
        statements = []

        def run():
            from sqlalchemy import event

            def capture(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement)

            engine = session.get_bind()
            event.listen(engine, "before_cursor_execute", capture)
            try:
                results.extend(recent_postings_query(session, inbox, sub, limit=1))
            finally:
                event.remove(engine, "before_cursor_execute", capture)

        plan = plan_of(
            session,
            run,
            "WITH recent_candidates",
            required_sql=("INDEXED BY ix_articles_date", "a.date >="),
        )
    assert "ix_articles_patch_series_key" in plan, plan
    assert not any("WITH candidates AS" in sql for sql in statements)
    assert len(results) == 1
    posting = results[0]
    assert posting.representative.message_id == "recent-resend@x"
    assert posting.matching_count == 2
    assert [p.message_id for p in posting.children] == ["original@x", "undated@x"]


def test_recent_selection_falls_back_for_old_and_undated_postings(seeded_db):
    with seeded_db() as session:
        sub = _add_subsystem(session, "QUIET", None, ["drivers/demo/"])
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        for index in range(35):
            article = _patch(session, f"quiet-{index}@x", key=f"quiet-{index}")
            article.date = datetime.now(UTC) - timedelta(days=60 + index)
        article = _patch(session, "quiet-undated@x", key="undated")
        article.date = None
        session.commit()
        rows = recent_postings_query(session, inbox, sub, limit=40)
    assert len(rows) == 36
    assert [p.representative.message_id for p in rows[:2]] == ["quiet-0@x", "quiet-1@x"]
    assert rows[-1].representative.message_id == "quiet-undated@x"


def test_recent_selection_keeps_future_dated_postings(seeded_db):
    with seeded_db() as session:
        sub = _add_subsystem(session, "FUTURE", None, ["drivers/demo/"])
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        article = _patch(session, "future@x")
        article.date = datetime.now(UTC) + timedelta(days=60)
        session.commit()
        rows = recent_postings_query(session, inbox, sub, limit=1)
    assert [p.representative.message_id for p in rows] == ["future@x"]


def test_unsupported_paths_return_empty_before_limit_validation(seeded_db):
    with seeded_db() as session:
        sub = _add_subsystem(session, "UNSUPPORTED", None, ["drivers/*/a.c"])
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        assert recent_postings_query(session, inbox, sub, limit=0) == []
