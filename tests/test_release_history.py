"""Closed releases use tag identities and reachability, never commit dates."""

from datetime import UTC, datetime

import pytest
from dulwich.objects import Tag, Tree
from dulwich.repo import Repo

from tests._helpers import add_commit


@pytest.fixture
def releases(tmp_path):
    path = tmp_path / "linus.git"
    repo = Repo.init_bare(path, mkdir=True)
    baseline = add_commit(repo, b"Linus <linus@x>", [], b"base", when=100000).id
    patch = add_commit(repo, b"Linus <linus@x>", [baseline], b"patch", when=1).id
    rc = add_commit(repo, b"Linus <linus@x>", [patch], b"rc", when=200000).id
    final = add_commit(repo, b"Linus <linus@x>", [rc], b"final", when=300000).id
    repo.refs[b"refs/tags/v6.19"] = baseline
    repo.refs[b"refs/tags/v7.0-rc1"] = rc
    tag = Tag()
    tag.tagger = b"Linus <linus@x>"
    tag.tag_time, tag.tag_timezone = 400000, 0
    tag.name, tag.message = b"v7.0", b"Release"
    tag.object = (type(repo[final]), final)
    repo.object_store.add_object(tag)
    repo.refs[b"refs/tags/v7.0"] = tag.id
    repo.refs[b"refs/tags/not-a-release"] = final
    repo.close()
    return path, baseline, patch, rc, final


def test_range_uses_reachability_and_tag_date(releases):
    from mimir.release_history import calculate_git, release_catalog

    path, baseline, patch, rc, final = releases
    catalog = release_catalog(path)
    assert [r.tag for r in catalog] == ["v7.0", "v6.19"]
    facts, shas = calculate_git(catalog[0])
    assert set(shas) == {s.decode() for s in (patch, rc, final)}
    assert baseline.decode() not in shas
    assert facts.commit_count == 3
    assert facts.release_date == datetime.fromtimestamp(400000, UTC)
    assert facts.date_source == "tagger"
    assert facts.rc_count == 1
    assert facts.merge_days == 1
    assert facts.stabilization_days == 2


def test_tree_and_missing_predecessor_are_unavailable(releases):
    from mimir.release_history import calculate_git, release_catalog

    path, *_ = releases
    with Repo(str(path)) as repo:
        tree = Tree()
        repo.object_store.add_object(tree)
        repo.refs[b"refs/tags/v2.6.11"] = tree.id
    catalog = release_catalog(path)
    tree_facts, members = calculate_git(catalog[-1])
    assert members is None
    assert tree_facts.release_date is None
    assert tree_facts.commit_count is None
    first, members = calculate_git(catalog[-2])
    assert members is None
    assert first.release_date is not None
    assert first.date_source == "commit"
    assert first.merge_days is None


def test_identity_changes_when_a_tag_moves(releases):
    from mimir.release_history import release_catalog

    path, baseline, *_ = releases
    old = release_catalog(path)[0].key
    with Repo(str(path)) as repo:
        repo.refs[b"refs/tags/v7.0-rc1"] = baseline
    assert release_catalog(path)[0].key != old


@pytest.mark.parametrize("tag", ["v7.0-rc1", "v7.0\n", "../v7.0", "7.0"])
def test_url_builder_rejects_nonrelease_tags(tag):
    from mimir.release_history import release_history_path

    with pytest.raises(ValueError):
        release_history_path(tag)


def test_git_cache_does_not_follow_walk_state(releases, seeded_db, monkeypatch):
    from mimir import release_history as history
    from mimir.models import MainlineState

    release = history.release_catalog(releases[0])[0]
    before = history.git_facts(release)
    with seeded_db() as session:
        session.add(MainlineState(tree_name="linus", last_walked_at=datetime.now(UTC)))
        session.commit()

    def unexpected(*args, **kwargs):
        pytest.fail("closed Git facts were recalculated on a cache hit")

    monkeypatch.setattr(history, "calculate_git", unexpected)
    assert history.git_facts(release) == before
    assert history.cached_facts([release])[release.tag] == before[0]


def test_cold_index_lists_every_release_without_calculation(
    client, releases, monkeypatch
):
    from mimir import release_history as history

    monkeypatch.setattr(history, "_linus_path", lambda: releases[0])

    def unexpected(*args, **kwargs):
        pytest.fail("history index must never walk Git")

    monkeypatch.setattr(history, "calculate_git", unexpected)
    response = client.get(history.release_history_path())
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    for release in history.release_catalog():
        assert f'href="{history.release_history_path(release.tag)}"' in body
    assert "Not calculated yet" in body
    assert response.headers["Cache-Control"] == "public, max-age=300"
    assert "ETag" not in response.headers


def test_history_sitemap_and_canonical_share_url_bytes(client, releases, monkeypatch):
    import re
    import xml.etree.ElementTree as ET

    from mimir import release_history as history

    monkeypatch.setattr(history, "_linus_path", lambda: releases[0])
    body = client.get(history.release_history_path()).get_data(as_text=True)
    canonical = re.search(r'<link rel="canonical" href="([^"]+)"', body).group(1)
    sitemap = ET.fromstring(client.get("/meta-sitemap.xml").data)
    locations = [n.text for n in sitemap.findall("{*}url/{*}loc")]
    assert canonical in locations
    base = canonical.removesuffix(history.release_history_path())
    for release in history.release_catalog():
        assert base + history.release_history_path(release.tag) in locations


def test_git_timeout_is_bounded_and_never_cached_as_zero(releases, monkeypatch):
    import subprocess

    from mimir import release_history as history

    release = history.release_catalog(releases[0])[0]
    calls = []

    def timeout(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(history.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        history.git_facts(release)
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == history.GIT_TIMEOUT_SEC
    assert history.cached_facts([release]) == {}


def test_archive_page_is_deduplicated_and_resolves_current_thread(
    seeded_db, monkeypatch
):
    from sqlalchemy import select

    from mimir import release_history as history
    from mimir.models import Article, Inbox

    mids = ["art3@example.com", "art4@example.com", "absent@x", "art3@example.com"]
    monkeypatch.setattr(history, "PAGE_SIZE", 1)
    with seeded_db() as session:
        rows, count = history.archived_page(session, mids, 1)
        assert count == 2
        assert len(rows) == 1
        assert rows[0][0].message_id == "art3@example.com"
        assert rows[0][1].startswith("/alpha/")
        article = session.scalar(select(Article).where(Article.message_id == mids[0]))
        article.canonical_inbox_id = session.scalar(
            select(Inbox.id).where(Inbox.name == "beta")
        )
        article.author = "Current Name <private@elsewhere.example>"
        session.commit()
    with seeded_db() as session:
        rows, count = history.archived_page(session, mids, 1)
        assert rows[0][1].startswith("/beta/")
        assert rows[0][0].author == "Current Name <private@elsewhere.example>"
        rows, count = history.archived_page(session, mids, 2)
        assert len(rows) == 1
        assert rows[0][0].message_id == "art4@example.com"
        root = session.scalar(
            select(Article.id).where(Article.message_id == "art1@example.com")
        )
        assert rows[0][1] == f"/alpha/2024/01/{root}/t"


def test_link_summary_has_one_day_lifetime_separate_from_git(releases, monkeypatch):
    from mimir import cache
    from mimir import release_history as history

    release = history.release_catalog(releases[0])[0]
    now = cache._now()
    # Freeze the clock: the Git walk can cross a second boundary, and a
    # write stamped one second late has not expired at now + TTL + 1.
    monkeypatch.setattr(cache, "_now", lambda: now)
    history.git_facts(release)
    history._link_payload(release, 3, ["a@x", "b@x"])
    assert history.cached_links([release])[release.tag].pair_count == 3
    monkeypatch.setattr(cache, "_now", lambda: now + history.LINKS_TTL_SEC + 1)
    assert history.cached_links([release]) == {}
    assert release.tag in history.cached_facts([release])


def test_recorded_links_use_membership_and_count_pairs(seeded_db):
    from mimir import release_history as history
    from mimir.models import MainlineCommit

    with seeded_db() as session:
        for sha, mid, tree in [
            ("a" * 40, "same@x", "linus"),
            ("b" * 40, "same@x", "linus"),
            ("b" * 40, "missing@x", "linus"),
            ("a" * 40, "other-tree@x", "linux-next"),
            ("c" * 40, "outside@x", "linus"),
        ]:
            session.add(
                MainlineCommit(
                    commit_sha=sha,
                    message_id=mid,
                    tree_name=tree,
                    committed_at=datetime(1900, 1, 1, tzinfo=UTC),
                )
            )
        session.commit()
        count, mids = history.calculate_links(session, ["a" * 40, "b" * 40])
        assert count == 3
        assert mids == ["missing@x", "same@x"]


def test_release_data_refreshes_links_after_one_day_not_a_walk(
    releases, seeded_db, monkeypatch
):
    from mimir import cache
    from mimir import release_history as history
    from mimir.models import MainlineCommit

    release = history.release_catalog(releases[0])[0]
    now = cache._now()
    facts, summary, mids = history.release_data(release)
    assert summary.pair_count == 0
    with seeded_db() as session:
        session.add(
            MainlineCommit(
                commit_sha=releases[2].decode(),
                message_id="later@x",
                tree_name="linus",
                committed_at=datetime.now(UTC),
            )
        )
        session.commit()
    assert history.release_data(release)[1].pair_count == 0
    monkeypatch.setattr(cache, "_now", lambda: now + history.LINKS_TTL_SEC + 2)
    monkeypatch.setattr(
        history,
        "calculate_git",
        lambda release: pytest.fail("Git cache must survive link refresh"),
    )
    _, summary, mids = history.release_data(release)
    assert summary.pair_count == 1
    assert mids == ["later@x"]


def test_detail_counts_references_and_links_current_archived_messages(
    client, releases, seeded_db, monkeypatch
):
    from mimir import release_history as history
    from mimir.models import MainlineCommit

    monkeypatch.setattr(history, "_linus_path", lambda: releases[0])
    monkeypatch.setattr(history, "PAGE_SIZE", 1)
    with seeded_db() as session:
        for sha, mid in [
            (releases[2], "art3@example.com"),
            (releases[3], "art3@example.com"),
            (releases[3], "art4@example.com"),
            (releases[4], "not-archived@x"),
        ]:
            session.add(
                MainlineCommit(
                    commit_sha=sha.decode(),
                    message_id=mid,
                    tree_name="linus",
                    committed_at=datetime.now(UTC),
                )
            )
        session.commit()
    response = client.get(history.release_history_path("v7.0"))
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "4 recorded mailing-list references" in body
    assert "2 distinct archived messages" in body
    assert "cross-posted note" in body
    assert "not-archived@x" not in body
    assert 'href="/merge-window/history/v7.0?page=2"' in body
    response = client.get("/merge-window/history/v7.0?page=2")
    assert response.status_code == 200
    assert "Re: hello alpha" in response.get_data(as_text=True)
    assert (
        'href="http://localhost/merge-window/history/v7.0?page=2"'
        in response.get_data(as_text=True)
    )
    assert "ETag" not in response.headers
    assert client.get("/merge-window/history/v7.0?page=3").status_code == 404


@pytest.mark.parametrize("path", ["v99.0", "v7.0-rc1", "v7.0?page=0", "v7.0?page=wat"])
def test_detail_rejects_unknown_tags_and_invalid_pages(
    client, releases, monkeypatch, path
):
    from mimir import release_history as history

    monkeypatch.setattr(history, "_linus_path", lambda: releases[0])
    assert client.get("/merge-window/history/" + path).status_code == 404


def test_slow_warm_target_calculates_pending_release_and_ends_read_transaction(
    releases, seeded_db, monkeypatch
):
    from mimir import release_history as history
    from mimir.cli.cache import _build_slow_global_targets

    monkeypatch.setattr(history, "_linus_path", lambda: releases[0])
    target = dict(_build_slow_global_targets())["release_history:v7.0"]
    real = history.calculate_git
    with seeded_db() as session:

        def checked(release):
            assert not session.in_transaction()
            return real(release)

        monkeypatch.setattr(history, "calculate_git", checked)
        from sqlalchemy import text

        session.execute(text("SELECT 1"))
        assert session.in_transaction()
        target(session)
        assert not session.in_transaction()
    release = history.release_catalog()[0]
    assert history.cached_facts([release])[release.tag].commit_count == 3
    assert history.cached_links([release])[release.tag].pair_count == 0


def test_cold_release_warm_walks_git_once_before_async_writes_land(
    releases, seeded_db, monkeypatch
):
    import threading

    from mimir import cache
    from mimir import release_history as history
    from mimir.broker import _context
    from mimir.broker.writes import WriteOp

    release = history.release_catalog(releases[0])[0]
    calculate = history.calculate_git
    calls = []

    def counted(release):
        calls.append(release.tag)
        return calculate(release)

    monkeypatch.setattr(history, "calculate_git", counted)
    writer = _context.get_active_writer()
    entered, proceed = threading.Event(), threading.Event()

    def hold_writer(conn):
        entered.set()
        assert proceed.wait(10), "test failed to release writer"

    held = writer.submit(WriteOp("review hold", hold_writer))
    assert entered.wait(2)
    active = cache._broker_handler_active()
    cache._set_broker_handler_active(True)
    try:
        facts, summary, mids = history.release_data(release)
        assert facts.commit_count == 3
        assert summary.pair_count == 0
        assert mids == []
    finally:
        cache._set_broker_handler_active(active)
        proceed.set()
        held.result(timeout=2)
        writer.submit(WriteOp("review drain", lambda conn: None)).result(timeout=2)
    assert calls == [release.tag], (
        "one cold release request must perform only one Git walk"
    )


def test_archive_pagination_does_not_offer_an_empty_undated_page(
    seeded_db, monkeypatch
):
    from sqlalchemy import select

    from mimir import release_history as history
    from mimir.models import Article

    mids = ["art3@example.com", "art4@example.com"]
    monkeypatch.setattr(history, "PAGE_SIZE", 1)
    with seeded_db() as session:
        undated = session.scalar(select(Article).where(Article.message_id == mids[1]))
        assert undated.date is not None
        undated.date = None
        session.commit()
    with seeded_db() as session:
        rows, total = history.archived_page(session, mids, 1)
        assert len(rows) == 1
        assert total == 1, (
            "only addressable dated messages may produce pagination pages"
        )


def test_warmer_repairs_a_missing_small_summary_without_rewalking(
    releases, seeded_db, monkeypatch
):
    from mimir import cache
    from mimir import release_history as history

    release = history.release_catalog(releases[0])[0]
    _, summary, _ = history.release_data(release)
    assert release.tag in history.cached_links([release])
    cache.delete(release.key + ":links")
    assert history.cached_links([release]) == {}
    assert cache.get(release.key + ":links_data") is not None

    def unexpected(release):
        raise AssertionError(
            "persisted data should repair the index without a Git walk"
        )

    monkeypatch.setattr(history, "calculate_git", unexpected)
    with seeded_db() as session:
        history.warm_release(session, release)
    assert history.cached_links([release]).get(release.tag) == summary


@pytest.mark.parametrize("production_stats", [False, True])
def test_recorded_lookup_is_pinned_to_sha_index_with_or_without_statistics(
    seeded_db, production_stats
):
    from mimir import release_history as history
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    with planner_stats(seeded_db, PRODUCTION_STATS if production_stats else ()):
        with seeded_db() as session:
            plan = plan_of(
                session,
                lambda: history.calculate_links(
                    session, [f"{n:040x}" for n in range(900)]
                ),
                "FROM mainline_commits",
                required_sql=("INDEXED BY sqlite_autoindex_mainline_commits_1",),
            )
            assert (
                "COVERING INDEX sqlite_autoindex_mainline_commits_1 (commit_sha=?)"
                in plan
            )
            assert "ix_mainline_commits_tree_name" not in plan


def test_detail_cache_hits_obey_current_privacy_and_canonical_inbox(
    client, releases, seeded_db, monkeypatch
):
    from sqlalchemy import select

    from mimir import release_history as history
    from mimir.config import settings
    from mimir.models import Article, Inbox, MainlineCommit

    monkeypatch.setattr(history, "_linus_path", lambda: releases[0])
    private = "private@elsewhere.example"
    monkeypatch.setattr(settings, "email_allowlist", [private])
    with seeded_db() as session:
        article = session.scalar(
            select(Article).where(Article.message_id == "art3@example.com")
        )
        article.author = private
        article_id = article.id
        session.add(
            MainlineCommit(
                commit_sha=releases[2].decode(),
                message_id=article.message_id,
                tree_name="linus",
                committed_at=datetime.now(UTC),
            )
        )
        session.commit()
    path = history.release_history_path("v7.0")
    before = client.get(path).get_data(as_text=True)
    assert private in before
    assert f'href="/alpha/2024/03/{article_id}/t"' in before
    monkeypatch.setattr(settings, "email_allowlist", [])
    with seeded_db() as session:
        session.get(Article, article_id).canonical_inbox_id = session.scalar(
            select(Inbox.id).where(Inbox.name == "beta")
        )
        session.commit()
    monkeypatch.setattr(
        history,
        "calculate_links",
        lambda *args: pytest.fail("expected linked-data cache hit"),
    )
    after = client.get(path).get_data(as_text=True)
    assert private not in after
    assert f'href="/beta/2024/03/{article_id}/t"' in after


def test_missing_git_is_retryable_not_a_false_empty_release(
    client, releases, monkeypatch
):
    import subprocess

    from mimir import release_history as history

    monkeypatch.setattr(history, "_linus_path", lambda: releases[0])

    def timeout(*args):
        raise subprocess.TimeoutExpired("git", 30)

    monkeypatch.setattr(history, "calculate_git", timeout)
    response = client.get(history.release_history_path("v7.0"))
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"
    assert response.headers["Cache-Control"] == "no-store"
    assert "0 recorded" not in response.get_data(as_text=True)


def test_small_summary_repair_cannot_extend_its_original_lifetime(
    releases, monkeypatch
):
    from mimir import cache
    from mimir import release_history as history

    release = history.release_catalog(releases[0])[0]
    _, summary, _ = history.release_data(release)
    cache.delete(release.key + ":links")
    monkeypatch.setattr(cache, "_now", lambda: summary.computed_at.timestamp() + 1000)
    history.release_data(release)
    assert history.cached_links([release])[release.tag] == summary
    monkeypatch.setattr(
        cache,
        "_now",
        lambda: summary.computed_at.timestamp() + history.LINKS_TTL_SEC + 1,
    )
    assert history.cached_links([release]) == {}
