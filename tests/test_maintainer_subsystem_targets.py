"""Warmed maintainer card links share the directory target rule."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from mimir import cache, directories
from mimir.mailmap import rules_version
from mimir.models import Inbox, MainlineState, SubsystemList
from mimir.subsystems_dashboard.activity import SubsystemActivity
from tests._dom import parse
from tests.test_subsystems._helpers import _add_recent_thread_root, _add_subsystem


def seed(session):
    subs = []
    for name, files in (("ACTIVE", ["active/"]), ("QUIET", []), ("UNMIRRORED", [])):
        subs.append(
            _add_subsystem(
                session,
                name,
                "Maintained",
                files=files,
                maintainers=[("M", "Alice", "alice@kernel.org")],
            )
        )
    for inbox in session.scalars(select(Inbox)):
        inbox.list_address = f"{inbox.name}@example.com"
        for sub in subs[:2]:
            session.add(SubsystemList(subsystem_id=sub.id, address=inbox.list_address))
    _add_recent_thread_root(
        session, "target-active@x", ["active/a.c"], inbox_name="beta"
    )
    session.commit()
    return subs


def cards(client):
    response = client.get("/maintainers/alice@kernel.org")
    assert response.status_code == 200
    return parse(response.get_data(as_text=True)).find(
        "ul", cls="maintainer-subsystems"
    )


def test_profile_links_match_directory_and_sitemap(client, session):
    seed(session)
    from mimir.subsystems_dashboard.activity import _most_active_subsystems_global_full

    _most_active_subsystems_global_full(session)
    rows, available = directories.subsystem_directory_rows(session)
    assert available
    expected = {r["name"].lower(): r["path"] for r in rows if r["path"]}
    assert expected == {
        "active": "/beta/subsystems/active/",
        "quiet": "/alpha/subsystems/quiet/",
    }
    cache.set(
        f"subsystem_targets:{rules_version(session)}",
        {r["name"]: r["path"] for r in rows if r["path"]},
        3600,
    )
    rendered = cards(client)
    assert {a.text(): a.attrs["href"] for a in rendered.find_all("a")} == expected
    assert len(rendered.find_all("li")) == 3
    assert all("Maintained" in row.text() for row in rendered.find_all("li"))
    assert set(expected.values()) <= set(directories.directory_sitemap_paths(session))


@pytest.mark.parametrize("have_map", [False, True])
def test_profile_targets_never_build_directory_or_activity(
    client, session, monkeypatch, have_map
):
    seed(session)
    cards(client)
    from mimir.subsystems_dashboard.activity import _most_active_subsystems_global_full

    _most_active_subsystems_global_full(session)
    if have_map:
        cache.set(
            f"subsystem_targets:{rules_version(session)}",
            {"ACTIVE": "/beta/subsystems/active/"},
            3600,
        )

    def forbidden(*args, **kwargs):
        pytest.fail("profile request built directory or activity")

    monkeypatch.setattr(directories, "_rules_snapshot", forbidden)
    monkeypatch.setattr(directories, "subsystem_directory_rows", forbidden)
    monkeypatch.setattr(directories, "_most_active_subsystems_global_full", forbidden)
    from mimir.web.routes import directory_pages

    monkeypatch.setattr(directory_pages, "_directory_rows", forbidden)
    assert len(cards(client).find_all("a")) == int(have_map)


@pytest.mark.parametrize("expired", [False, True])
def test_profile_hides_map_when_ranking_cold(client, session, expired):
    seed(session)
    version = rules_version(session)
    cache.set(
        f"subsystem_targets:{version}", {"ACTIVE": "/beta/subsystems/active/"}, 3600
    )
    if expired:
        cache.set(f"most_active_subsystems_global:v2:7:{version}", [], -1)
    assert not cache.alive(session, f"most_active_subsystems_global:v2:7:{version}")
    assert cache.get(f"subsystem_targets:{version}") is not None
    assert cards(client).find_all("a") == []


def test_profile_links_with_available_empty_ranking(client, session):
    seed(session)
    version = rules_version(session)
    cache.set(f"most_active_subsystems_global:v2:7:{version}", [], 3600)
    cache.set(
        f"subsystem_targets:{version}", {"QUIET": "/alpha/subsystems/quiet/"}, 3600
    )
    assert [a.attrs["href"] for a in cards(client).find_all("a")] == [
        "/alpha/subsystems/quiet/"
    ]


def test_warm_uses_returned_ranking_before_cache_write_visible(session, monkeypatch):
    active, _, _ = seed(session)
    calls = []
    ranking = [SubsystemActivity(active.id, active.name, "beta", 1, datetime.now(UTC))]

    def ranked(session):
        calls.append(True)
        return ranking

    monkeypatch.setattr(directories, "_most_active_subsystems_global_full", ranked)
    assert (
        cache.get(f"most_active_subsystems_global:v2:7:{rules_version(session)}")
        is None
    )
    directories.warm_subsystem_targets(session)
    assert calls == [True]
    assert cache.get(f"subsystem_targets:{rules_version(session)}") == {
        "ACTIVE": "/beta/subsystems/active/",
        "QUIET": "/alpha/subsystems/quiet/",
    }


def test_warm_discards_map_when_rules_change(session, monkeypatch):
    seed(session)
    state = MainlineState(tree_name="linus", last_commit_sha="before")
    session.add(state)
    session.commit()

    def changed(session):
        state.last_commit_sha = "after"
        session.commit()
        return []

    monkeypatch.setattr(directories, "_most_active_subsystems_global_full", changed)
    directories.warm_subsystem_targets(session)
    assert cache.get("subsystem_targets:before") is None
    assert cache.get("subsystem_targets:after") is None


@pytest.mark.parametrize("mutation", ["rename", "list_address", "delete"])
def test_inbox_mutations_invalidate_global_targets(session, client, mutation):
    seed(session)
    from mimir.inboxes import delete_inbox, update_inbox
    from mimir.subsystems_dashboard.activity import _most_active_subsystems_global_full

    _most_active_subsystems_global_full(session)
    key = f"subsystem_targets:{rules_version(session)}"
    cache.set(key, {"ACTIVE": "/beta/subsystems/active/"}, 3600)
    if mutation == "rename":
        update_inbox("beta", new_name="renamed")
    elif mutation == "list_address":
        update_inbox("beta", list_address="new@example.com")
    else:
        delete_inbox("beta", keep_orphan_articles=True)
    assert cache.get(key) is None
    assert cards(client).find_all("a") == []


def test_slow_and_reload_warm_share_composed_target(session, monkeypatch):
    from mimir.broker.handlers import maintenance, warm
    from mimir.cli.cache import _build_slow_global_targets

    label = "most_active_subsystems_global (7d)"
    assert (
        dict(_build_slow_global_targets())[label] is directories.warm_subsystem_targets
    )
    seen = []
    monkeypatch.setattr(
        warm,
        "_run_targets",
        lambda targets, **kwargs: seen.extend(targets) or ([], [], 0, []),
    )
    maintenance._warm_activity_after_reload()
    assert dict(seen)[label] is directories.warm_subsystem_targets


@pytest.mark.parametrize("have_map", [False, True])
def test_profile_target_reads_have_bounded_sql(client, session, monkeypatch, have_map):
    from sqlalchemy import event

    from mimir.web.routes import maintainers

    seed(session)
    cards(client)
    version = rules_version(session)
    cache.set(f"most_active_subsystems_global:v2:7:{version}", [], 3600)
    if have_map:
        cache.set(
            f"subsystem_targets:{version}", {"QUIET": "/alpha/subsystems/quiet/"}, 3600
        )
    statements = []
    engine = session.get_bind()

    def capture(conn, cursor, sql, params, context, many):
        statements.append((sql, params))

    def counted():
        statements.clear()
        cards(client)
        return len(statements)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with monkeypatch.context() as baseline:
            baseline.setattr(
                maintainers, "cached_subsystem_targets", lambda session: {}
            )
            before = counted()
        after = counted()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert after - before == (3 if have_map else 2)
    source_key = cache._ns(f"most_active_subsystems_global:v2:7:{version}")
    ranking_reads = [sql for sql, params in statements if source_key in params]
    assert len(ranking_reads) == int(have_map)
    if have_map:
        assert "cache.expires_at" in ranking_reads[0]
        assert "cache.value" not in ranking_reads[0]


def test_directory_discards_activity_if_snapshot_moves_generation(session, monkeypatch):
    active, _, _ = seed(session)
    state = MainlineState(tree_name="linus", last_commit_sha="before")
    session.add(state)
    session.commit()
    ranking = [SubsystemActivity(active.id, active.name, "beta", 1, datetime.now(UTC))]
    monkeypatch.setattr(
        directories,
        "_most_active_subsystems_global_full",
        lambda session, **kwargs: ranking,
    )
    calls = []

    def replaced_snapshot(session):
        calls.append(True)
        state.last_commit_sha = "after"
        active.name = "NEW TAXONOMY NAME"
        session.commit()
        return directories._compute_rules_snapshot()

    monkeypatch.setattr(directories, "_rules_snapshot", replaced_snapshot)
    assert directories.subsystem_directory_rows(session) == ([], False)
    assert calls == [True]


@pytest.mark.parametrize("mutation", ["rules", "inbox"])
def test_warm_discards_targets_if_metadata_changes_before_publish(
    session, monkeypatch, mutation
):
    active, _, _ = seed(session)
    state = MainlineState(tree_name="linus", last_commit_sha="before")
    session.add(state)
    session.commit()
    ranking = [SubsystemActivity(active.id, active.name, "beta", 1, datetime.now(UTC))]
    monkeypatch.setattr(
        directories, "_most_active_subsystems_global_full", lambda session: ranking
    )
    original = directories._subsystem_directory_rows
    calls = []

    def changed(session, version, activity):
        result = original(session, version, activity)
        assert result[1] and result[0]
        calls.append(True)
        if mutation == "rules":
            state.last_commit_sha = "after"
            session.commit()
        else:
            from mimir.inboxes import update_inbox

            update_inbox("beta", new_name="renamed")
        return result

    monkeypatch.setattr(directories, "_subsystem_directory_rows", changed)
    directories.warm_subsystem_targets(session)
    assert calls == [True]
    assert cache.get("subsystem_targets:before") is None
    assert cache.get("subsystem_targets:after") is None
