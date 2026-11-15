"""Cross-inbox messages authored and carried review trailers for current M entries."""

from datetime import UTC, datetime, timedelta

from mimir import maintainer_activity
from mimir.models import Article, ArticleTrailer, Mailmap
from tests.test_maintainer_directory import _add_subsystem


def test_counts_current_authors_one_hop_and_raw_review_rows(session, monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    monkeypatch.setattr(maintainer_activity, "_now", lambda: now)
    _add_subsystem(
        session,
        "TEST",
        "Maintained",
        [
            ("M", "Alice", "Alice@example.com"),
            ("M", "", "bob@example.com"),
            ("R", "Reviewer", "reviewer@example.com"),
            ("M", "Invalid", "bad/address@example.com"),
            ("M", "No messages", "quiet@example.com"),
        ],
    )
    session.add(Mailmap(old_address="old@example.com", address="alice@example.com"))
    session.add(Mailmap(old_address="ancient@example.com", address="old@example.com"))
    authors = [
        "Alice <OLD@EXAMPLE.COM>",
        "alice@example.com",
        "bob@example.com",
        "reviewer@example.com",
        "bad/address@example.com",
        "ancient@example.com",
        None,
        "broken",
    ]
    articles = [
        Article(message_id=f"activity-{i}", author=author, date=now - timedelta(days=1))
        for i, author in enumerate(authors)
    ]
    session.add_all(articles)
    session.flush()
    for role in (
        "Reviewed-by",
        "Reviewed-by",
        "Acked-by",
        "Tested-by",
        "Signed-off-by",
    ):
        session.add(
            ArticleTrailer(
                article_id=articles[0].id,
                role=role,
                name="Old",
                address="old@example.com",
                address_normalized="old@example.com",
            )
        )
    session.add(
        ArticleTrailer(
            article_id=articles[0].id,
            role="Reviewed-by",
            name="Quiet",
            address="quiet@example.com",
            address_normalized="quiet@example.com",
        )
    )
    session.commit()
    assert maintainer_activity.most_active_maintainers(session, force=True) == [
        {
            "address": "alice@example.com",
            "name": "Alice",
            "message_count": 2,
            "review_count": 4,
        },
        {
            "address": "bob@example.com",
            "name": "bob@example.com",
            "message_count": 1,
            "review_count": 0,
        },
    ]


def test_interval_is_half_open_for_messages_and_trailers(session, monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    monkeypatch.setattr(maintainer_activity, "_now", lambda: now)
    _add_subsystem(session, "BOUNDS", "Maintained", [("M", "Alice", "a@example.com")])
    dates = [
        None,
        now - timedelta(days=7, microseconds=1),
        now - timedelta(days=7),
        now - timedelta(microseconds=1),
        now,
        now + timedelta(days=1),
    ]
    for i, date in enumerate(dates):
        article = Article(message_id=f"bound-{i}", author="a@example.com", date=date)
        session.add(article)
        session.flush()
        session.add(
            ArticleTrailer(
                article_id=article.id,
                role="Reviewed-by",
                name="A",
                address="a@example.com",
                address_normalized="a@example.com",
            )
        )
    session.commit()
    (row,) = maintainer_activity.most_active_maintainers(session, force=True)
    assert (row["message_count"], row["review_count"]) == (2, 2)


def test_full_ranking_filters_before_limit_and_ties_by_address(session):
    entries = [("M", str(i), f"m{i:02}@example.com") for i in range(15)]
    entries.append(("M", "Invalid", "bad/address@example.com"))
    _add_subsystem(session, "RANK", "Maintained", entries)
    for i, (_, _, address) in enumerate(reversed(entries)):
        for n in range(20 if "/" in address else 1):
            session.add(
                Article(
                    message_id=f"rank-{i}-{n}",
                    author=address,
                    date=datetime.now(UTC) - timedelta(hours=1),
                )
            )
    session.flush()
    from sqlalchemy import select

    last_article = session.scalar(
        select(Article.id).where(Article.author == "m14@example.com")
    )
    session.add_all(
        [
            ArticleTrailer(
                article_id=last_article,
                role="Reviewed-by",
                name="Late",
                address="m14@example.com",
                address_normalized="m14@example.com",
            )
            for _ in range(20)
        ]
    )
    session.commit()
    first = maintainer_activity.most_active_maintainers(session, force=True)
    assert len(first) == 12
    assert [r["address"] for r in first] == [f"m{i:02}@example.com" for i in range(12)]
    assert (
        len(
            maintainer_activity.most_active_maintainers(
                session, limit=20, compute_on_miss=False
            )
        )
        == 15
    )


def test_cache_only_empty_force_days_and_warm_window(session, monkeypatch):
    from sqlalchemy import select

    from mimir import cache
    from mimir.models import CacheEntry

    assert (
        maintainer_activity.most_active_maintainers(session, compute_on_miss=False)
        == []
    )
    assert maintainer_activity.most_active_maintainers(session) == []
    _add_subsystem(session, "CACHE", "Maintained", [("M", "A", "a@example.com")])
    session.add(
        Article(
            message_id="cache-message",
            author="a@example.com",
            date=datetime.now(UTC) - timedelta(days=2),
        )
    )
    session.commit()
    assert (
        maintainer_activity.most_active_maintainers(session) == []
    )  # cached empty is a hit
    assert maintainer_activity.most_active_maintainers(session, days=1) == []
    with cache.warm_window(7200):
        rows = maintainer_activity.most_active_maintainers(session, force=True)
    assert len(rows) == 1
    entry = session.execute(
        select(CacheEntry).where(CacheEntry.key.like("%most_active_maintainers:v1:7:%"))
    ).scalar_one()
    assert 10795 <= entry.expires_at - cache._now() <= 10800
    monkeypatch.setattr(
        maintainer_activity,
        "all_maintainers",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("cold computation")),
    )
    assert (
        maintainer_activity.most_active_maintainers(session, compute_on_miss=False)
        == rows
    )
    assert (
        maintainer_activity.most_active_maintainers(
            session, days=30, compute_on_miss=False
        )
        == []
    )


def test_rules_generation_refreshes_directory_and_mapping(session):
    from mimir.models import MainlineState

    state = MainlineState(tree_name="linus", last_commit_sha="maintainers:mailmap")
    session.add(state)
    sub = _add_subsystem(
        session, "VERSIONS", "Maintained", [("M", "Old", "old@example.com")]
    )
    session.add(
        Article(
            message_id="version-message",
            author="old@example.com",
            date=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    session.commit()
    assert (
        maintainer_activity.most_active_maintainers(session)[0]["address"]
        == "old@example.com"
    )
    sub.maintainers[0].address = "new@example.com"
    sub.maintainers[0].name = "New"
    session.add(Mailmap(old_address="old@example.com", address="new@example.com"))
    state.last_commit_sha = "newmaintainers:newmailmap"
    session.commit()
    assert (
        maintainer_activity.most_active_maintainers(session, compute_on_miss=False)
        == []
    )
    assert maintainer_activity.most_active_maintainers(session)[0]["name"] == "New"
    state.last_commit_sha += ".forced-generation"
    session.commit()
    assert (
        maintainer_activity.most_active_maintainers(session, compute_on_miss=False)
        == []
    )
    assert (
        maintainer_activity.most_active_maintainers(session)[0]["address"]
        == "new@example.com"
    )


def test_competing_rules_reload_discards_computation(session, seeded_db, monkeypatch):
    from sqlalchemy import event, update

    from mimir import cache
    from mimir.models import MainlineState

    session.add(MainlineState(tree_name="linus", last_commit_sha="before"))
    _add_subsystem(session, "RACE", "Maintained", [("M", "A", "a@example.com")])
    session.add(
        Article(
            message_id="race-message",
            author="a@example.com",
            date=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    session.commit()
    published = []
    original_set = cache.set
    monkeypatch.setattr(
        cache,
        "set",
        lambda key, value, ttl: (published.append(key), original_set(key, value, ttl))[
            -1
        ],
    )
    changed = False

    def reload_after_authors(conn, cursor, statement, parameters, context, executemany):
        nonlocal changed
        if "SELECT author FROM articles" in statement and not changed:
            changed = True
            with seeded_db() as writer:
                writer.execute(update(MainlineState).values(last_commit_sha="after"))
                writer.commit()

    engine = session.get_bind()
    event.listen(engine, "after_cursor_execute", reload_after_authors)
    try:
        assert maintainer_activity.most_active_maintainers(session, force=True) == []
    finally:
        event.remove(engine, "after_cursor_execute", reload_after_authors)
    assert changed
    assert not any("most_active_maintainers" in key for key in published)
    assert (
        maintainer_activity.most_active_maintainers(session, compute_on_miss=False)
        == []
    )


def test_actual_queries_use_date_and_article_indexes(session, seeded_db):
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    with planner_stats(seeded_db, PRODUCTION_STATS):
        author_plan = plan_of(
            session,
            lambda: maintainer_activity.most_active_maintainers(session, force=True),
            "SELECT author FROM articles",
            required_sql=("INDEXED BY ix_articles_date",),
        )
        trailer_plan = plan_of(
            session,
            lambda: maintainer_activity.most_active_maintainers(session, force=True),
            "SELECT t.address_normalized",
            required_sql=("CROSS JOIN article_trailers",),
        )
    assert (
        "SEARCH articles USING INDEX ix_articles_date (date>? AND date<?)"
        in author_plan
    )
    assert (
        "SEARCH a USING COVERING INDEX ix_articles_date (date>? AND date<?)"
        in trailer_plan
    )
    assert (
        "SEARCH t USING INDEX ix_article_trailers_article_id (article_id=?)"
        in trailer_plan
    )
    assert "SCAN" not in author_plan + trailer_plan


def test_mailmap_cannot_rescue_missing_author_mailbox_parts(session):
    _add_subsystem(session, "RAW", "Maintained", [("M", "A", "a@example.com")])
    for i, address in enumerate(("broken", "@example.com", "o'brien@old.example")):
        session.add(Mailmap(old_address=address, address="a@example.com"))
        session.add(
            Article(
                message_id=f"raw-{i}",
                author=address,
                date=datetime.now(UTC) - timedelta(hours=1),
            )
        )
    session.commit()
    assert (
        maintainer_activity.most_active_maintainers(session, force=True)[0][
            "message_count"
        ]
        == 1
    )


def test_actual_forced_rules_reload_makes_old_ranking_cold(session, tmp_path):
    from mimir.mainline import load_maintainers
    from tests.test_mainline import _bare_repo, _commit_maintainers

    path = tmp_path / "activity.git"
    repo = _bare_repo(path)
    _commit_maintainers(
        repo, b"ACTIVITY\nM:\tA <a@example.com>\nF:\tfs/a/\n", ts=1700000000
    )
    repo.close()
    load_maintainers(path, tree_name="linus")
    session.add(
        Article(
            message_id="forced-activity",
            author="a@example.com",
            date=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    session.commit()
    first = maintainer_activity.most_active_maintainers(session)
    assert first[0]["message_count"] == 1
    load_maintainers(path, tree_name="linus", force=True)
    assert (
        maintainer_activity.most_active_maintainers(session, compute_on_miss=False)
        == []
    )
    assert maintainer_activity.most_active_maintainers(session) == first


def test_crossposting_does_not_multiply_messages_or_trailers(session):
    from sqlalchemy import func, select

    from mimir.models import ArticleList

    _add_subsystem(session, "CROSS", "Maintained", [("M", "A", "a@example.com")])
    article_id = session.scalar(
        select(ArticleList.article_id)
        .group_by(ArticleList.article_id)
        .having(func.count() > 1)
    )
    assert article_id is not None
    article = session.get(Article, article_id)
    article.author = "a@example.com"
    article.date = datetime.now(UTC) - timedelta(hours=1)
    session.add(
        ArticleTrailer(
            article_id=article_id,
            role="Reviewed-by",
            name="A",
            address="a@example.com",
            address_normalized="a@example.com",
        )
    )
    session.commit()
    (row,) = maintainer_activity.most_active_maintainers(session, force=True)
    assert (row["message_count"], row["review_count"]) == (1, 1)


def test_compute_has_constant_query_count_and_cache_only_two_reads(session):
    from sqlalchemy import event

    _add_subsystem(
        session,
        "QUERIES",
        "Maintained",
        [("M", str(i), f"m{i}@example.com") for i in range(20)],
    )
    session.add_all(
        [
            Article(
                message_id=f"queries-{i}",
                author=f"m{i}@example.com",
                date=datetime.now(UTC) - timedelta(hours=1),
            )
            for i in range(20)
        ]
    )
    session.commit()
    statements = []
    engine = session.get_bind()

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        maintainer_activity.most_active_maintainers(session, force=True)
        assert (
            len(statements) == 6
        )  # version, directory, mailmap, authors, trailers, version
        statements.clear()
        maintainer_activity.most_active_maintainers(session, compute_on_miss=False)
        assert len(statements) == 2  # version and cache only
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_warm_window_refreshes_without_force(session, monkeypatch):
    from mimir import cache, cache_warm

    _add_subsystem(session, "REFRESH", "Maintained", [("M", "A", "a@example.com")])
    session.commit()
    assert maintainer_activity.most_active_maintainers(session) == []
    session.add(
        Article(
            message_id="refresh-message",
            author="a@example.com",
            date=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    session.commit()
    seen = []

    def refresh(**kwargs):
        seen.append(kwargs)
        return True

    monkeypatch.setattr(cache_warm, "should_refresh", refresh)
    with cache.warm_window(7200):
        assert (
            maintainer_activity.most_active_maintainers(session)[0]["message_count"]
            == 1
        )
    assert len(seen) == 1
    assert seen[0]["nominal_ttl"] == 3600
    assert seen[0]["window_sec"] == 7200


def test_directory_only_then_mailmap_only_changes_rotate_cached_ranking(session):
    from mimir.models import MainlineState

    state = MainlineState(tree_name="linus", last_commit_sha="maintainers:mailmap")
    session.add(state)
    sub = _add_subsystem(
        session,
        "INDEPENDENT",
        "Maintained",
        [("M", "A", "a@example.com"), ("M", "B", "b@example.com")],
    )
    alias = Mailmap(old_address="old@example.com", address="a@example.com")
    session.add(alias)
    session.add(
        Article(
            message_id="independent-rules",
            author="old@example.com",
            date=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    session.commit()
    assert maintainer_activity.most_active_maintainers(session)[0]["name"] == "A"
    sub.maintainers[0].name = "Renamed A"
    state.last_commit_sha = "changedmaintainers:mailmap"
    session.commit()
    assert (
        maintainer_activity.most_active_maintainers(session, compute_on_miss=False)
        == []
    )
    assert (
        maintainer_activity.most_active_maintainers(session)[0]["name"] == "Renamed A"
    )
    alias.address = "b@example.com"
    state.last_commit_sha = "changedmaintainers:changedmailmap"
    session.commit()
    assert (
        maintainer_activity.most_active_maintainers(session, compute_on_miss=False)
        == []
    )
    (row,) = maintainer_activity.most_active_maintainers(session)
    assert (row["address"], row["message_count"]) == ("b@example.com", 1)
