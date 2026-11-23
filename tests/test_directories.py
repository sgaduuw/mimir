"""Complete directory membership, target selection and cache-only activity."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from mimir.directories import maintainer_directory_rows, subsystem_directory_rows
from mimir.maintainer_activity import maintainer_activity_rows, most_active_maintainers
from mimir.models import Article, ArticleTrailer, Inbox, SubsystemList
from mimir.subsystems import subsystem_path
from mimir.subsystems_dashboard import most_active_subsystems_global
from tests.test_subsystems._helpers import _add_recent_thread_root, _add_subsystem


def test_subsystem_directory_has_quiet_entries_and_one_defined_target(session):
    active = _add_subsystem(session, "ACTIVE", "Maintained", files=["active/"])
    quiet = _add_subsystem(session, "QUIET", "Maintained", files=["quiet/"])
    _add_subsystem(session, "UNMIRRORED", None, files=[])
    _add_subsystem(session, "BAD\tNAME", None, files=[])
    alpha = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
    beta = session.scalar(select(Inbox).where(Inbox.name == "beta"))
    alpha.list_address = "alpha@example.com"
    beta.list_address = "beta@example.com"
    for sub in (active, quiet):
        session.add(SubsystemList(subsystem_id=sub.id, address="alpha@example.com"))
        session.add(SubsystemList(subsystem_id=sub.id, address="beta@example.com"))
    _add_recent_thread_root(
        session, "directory-active@x", ["active/a.c"], inbox_name="beta"
    )
    session.commit()
    cold, available = subsystem_directory_rows(session)
    assert not available
    assert [r["name"] for r in cold] == ["ACTIVE", "QUIET", "UNMIRRORED"]
    assert all(r["message_count"] is None for r in cold)
    assert cold[0]["inbox_name"] == "alpha"  # lexical mirrored L: fallback
    most_active_subsystems_global(session, force=True)
    rows, available = subsystem_directory_rows(session)
    assert available
    assert [(r["name"], r["inbox_name"], r["message_count"]) for r in rows] == [
        ("ACTIVE", "beta", 1),
        ("QUIET", "alpha", 0),
        ("UNMIRRORED", None, 0),
    ]
    assert rows[0]["path"] == subsystem_path("beta", "ACTIVE")
    assert rows[2]["path"] is None


def test_maintainer_directory_includes_review_only_and_counts_distinct_subsystems(
    session,
):
    people = [
        ("M", "Alice", "ALICE@example.com"),
        ("M", "Alice", "alice@example.com"),
        ("R", "Reviewer", "r@example.com"),
        ("M", "Quiet", "quiet@example.com"),
    ]
    _add_subsystem(session, "ONE", None, files=[], maintainers=people)
    _add_subsystem(
        session,
        "TWO",
        None,
        files=[],
        maintainers=[("M", "Alice", "alice@example.com")],
    )
    article = Article(
        message_id="directory-review@x",
        author="someone@example.com",
        date=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(article)
    session.flush()
    session.add(
        ArticleTrailer(
            article_id=article.id,
            role="Reviewed-by",
            name="Alice",
            address="alice@example.com",
            address_normalized="alice@example.com",
        )
    )
    session.commit()
    cold, available = maintainer_directory_rows(session)
    assert not available
    assert [r["review_count"] for r in cold] == [None, None]
    assert maintainer_activity_rows(session, force=True)[0]["review_count"] == 1
    assert most_active_maintainers(session) == []  # front page remains authored-only
    rows, available = maintainer_directory_rows(session)
    assert available
    assert [(r["address"], r["subsystem_count"], r["review_count"]) for r in rows] == [
        ("alice@example.com", 2, 1),
        ("quiet@example.com", 1, 0),
    ]


def test_directory_refreshes_old_snapshot_before_post_reload_activity_join(session):
    from mimir.models import MainlineState
    from mimir.subsystems import _rules_snapshot

    state = MainlineState(tree_name="linus", last_commit_sha="old")
    session.add(state)
    sub = _add_subsystem(session, "OLD", None, files=["renamed/"])
    _add_recent_thread_root(session, "renamed@x", ["renamed/a.c"])
    session.commit()
    assert _rules_snapshot(session).subsystems[sub.id].name == "OLD"
    sub.name = "NEW"
    state.last_commit_sha = "new"
    session.commit()
    # Deliberately leave the old fixed-key snapshot cached: this is the
    # interval after the rule replacement commits but before invalidation.
    assert _rules_snapshot(session).subsystems[sub.id].name == "OLD"
    most_active_subsystems_global(session, force=True)
    rows, available = subsystem_directory_rows(session)
    assert available
    assert [(r["name"], r["message_count"]) for r in rows] == [("NEW", 1)]


def test_directory_requests_never_scan_activity_on_a_cold_cache(session):
    from sqlalchemy import event

    _add_subsystem(
        session, "COLD", None, files=[], maintainers=[("M", "Cold", "cold@example.com")]
    )
    session.commit()
    seen = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement.lower())

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert subsystem_directory_rows(session)[0]
        assert maintainer_directory_rows(session)[0]
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not any(
        "from articles" in sql
        or "from article_trailers" in sql
        or "from article_files" in sql
        for sql in seen
    )


def test_maintainer_activity_empty_is_available_and_old_schema_cache_is_cold(session):
    from mimir import cache

    cache.set("most_active_maintainers:v1:7:", [], 3600)
    assert maintainer_activity_rows(session, compute_on_miss=False) is None
    assert maintainer_activity_rows(session, force=True) == []
    assert maintainer_directory_rows(session) == ([], True)


def test_directory_discards_rows_when_reload_lands_during_last_metadata_read(
    session, seeded_db
):
    from sqlalchemy import event, update

    from mimir.models import MainlineState, Subsystem

    session.add(MainlineState(tree_name="linus", last_commit_sha="before"))
    sub = _add_subsystem(session, "OLD NAME", None, files=[])
    alpha = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
    alpha.list_address = "alpha@example.com"
    session.add(SubsystemList(subsystem_id=sub.id, address="alpha@example.com"))
    session.commit()
    changed = False

    def reload(conn, cursor, statement, parameters, context, executemany):
        nonlocal changed
        if "FROM subsystem_lists JOIN inboxes" in statement and not changed:
            changed = True
            with seeded_db() as writer:
                writer.execute(
                    update(Subsystem)
                    .where(Subsystem.id == sub.id)
                    .values(name="NEW NAME")
                )
                writer.execute(update(MainlineState).values(last_commit_sha="after"))
                writer.commit()

    engine = session.get_bind()
    event.listen(engine, "after_cursor_execute", reload)
    try:
        assert subsystem_directory_rows(session) == ([], False)
    finally:
        event.remove(engine, "after_cursor_execute", reload)
    assert changed


def test_sitemap_uses_directory_target_and_versions_changed_target(session):
    import xml.etree.ElementTree as ET

    from mimir.directory_paths import (
        maintainer_directory_path,
        subsystem_directory_path,
    )
    from mimir.seo.sitemaps import meta_sitemap_xml

    sub = _add_subsystem(
        session,
        "DIRECTORY",
        None,
        files=["directory/"],
        maintainers=[("M", "Alice", "alice@example.com")],
    )
    alpha = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
    alpha.list_address = "alpha@example.com"
    session.add(SubsystemList(subsystem_id=sub.id, address="alpha@example.com"))
    session.commit()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    def paths(payload):
        return {
            node.text.removeprefix("https://example.test")
            for node in ET.fromstring(payload.body).findall("s:url/s:loc", ns)
        }

    cold = paths(meta_sitemap_xml(session, "https://example.test"))
    assert {
        subsystem_directory_path(),
        subsystem_directory_path("d"),
        maintainer_directory_path(),
        maintainer_directory_path("a"),
        subsystem_path("alpha", "DIRECTORY"),
    } <= cold
    _add_recent_thread_root(
        session, "sitemap-directory@x", ["directory/a.c"], inbox_name="beta"
    )
    session.commit()
    most_active_subsystems_global(session, force=True)
    rows, available = subsystem_directory_rows(session)
    assert available
    assert rows[0]["path"] == subsystem_path("beta", "DIRECTORY")
    warm = paths(meta_sitemap_xml(session, "https://example.test"))
    assert rows[0]["path"] in warm
    assert subsystem_path("alpha", "DIRECTORY") not in warm


def test_sitemap_does_not_mix_directory_generations(session, seeded_db):
    from sqlalchemy import event, update

    from mimir import cache
    from mimir.directories import directory_sitemap_entries
    from mimir.models import MainlineState, Subsystem
    from mimir.subsystems_dashboard.activity import (
        SubsystemActivity,
        global_subsystem_activity_key,
    )

    session.add(MainlineState(tree_name="linus", last_commit_sha="before"))
    sub = _add_subsystem(
        session,
        "OLD",
        None,
        files=[],
        maintainers=[("M", "Owner", "owner@example.com")],
    )
    alpha = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
    alpha.list_address = "alpha@example.com"
    session.add(SubsystemList(subsystem_id=sub.id, address="alpha@example.com"))
    session.commit()
    cache.set(
        global_subsystem_activity_key("before"),
        [
            SubsystemActivity(
                sub.id, sub.name, "alpha", 1, datetime(2024, 6, 14, tzinfo=UTC)
            )
        ],
        3600,
    )
    changed = False

    def reload(conn, cursor, statement, parameters, context, executemany):
        nonlocal changed
        if "min(nullif(subsystem_maintainers.name" in statement and not changed:
            changed = True
            with seeded_db() as writer:
                writer.execute(
                    update(Subsystem).where(Subsystem.id == sub.id).values(name="NEW")
                )
                writer.execute(update(MainlineState).values(last_commit_sha="after"))
                writer.commit()

    engine = session.get_bind()
    event.listen(engine, "after_cursor_execute", reload)
    try:
        assert directory_sitemap_entries(session) == [
            ("/maintainers/", None),
            ("/subsystems/", None),
        ]
    finally:
        event.remove(engine, "after_cursor_execute", reload)
    assert changed
