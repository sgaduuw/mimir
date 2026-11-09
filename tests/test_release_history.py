"""Closed releases use tag identities and reachability, never commit dates."""

import re
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


def test_cold_index_lists_nothing_unchecked_and_never_calculates(
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
    catalog = history.release_catalog()
    assert catalog, "the fixture must provide releases for the index to omit"
    for release in catalog:
        assert f'href="{history.release_history_path(release.tag)}"' not in body
    assert "Not calculated yet" not in body
    # Nothing is checked yet, so the page says more may appear rather
    # than that no release has references.
    assert re.search(
        r"\b(\d+) of (\d+) releases have not been checked", body
    ).groups() == (
        str(len(catalog)),
        str(len(catalog)),
    )
    assert response.headers["Cache-Control"] == "public, max-age=300"
    assert "ETag" not in response.headers


def _record(seeded_db, sha: bytes, message_id: str) -> None:
    from mimir.models import MainlineCommit

    with seeded_db() as session:
        session.add(
            MainlineCommit(
                commit_sha=sha.decode(),
                message_id=message_id,
                tree_name="linus",
                committed_at=datetime.now(UTC),
            )
        )
        session.commit()


def _sitemap_locations(client) -> list[str]:
    import xml.etree.ElementTree as ET

    sitemap = ET.fromstring(client.get("/meta-sitemap.xml").data)
    return [n.text for n in sitemap.findall("{*}url/{*}loc")]


def _canonical(body: str) -> str:
    return re.search(r'<link rel="canonical" href="([^"]+)"', body).group(1)


def test_history_sitemap_and_canonical_share_url_bytes(
    client, releases, seeded_db, monkeypatch
):
    from mimir import release_history as history

    monkeypatch.setattr(history, "_linus_path", lambda: releases[0])
    _record(seeded_db, releases[2], "art3@example.com")
    for release in history.release_catalog():
        history.release_data(release)
    listed = history.recorded_history(history.release_catalog()).listed
    # An empty list would run the loop below zero times and pass.
    assert [release.tag for release, _f, _l in listed] == ["v7.0"]
    index = client.get(history.release_history_path()).get_data(as_text=True)
    locations = _sitemap_locations(client)
    assert _canonical(index) in locations
    for release, _facts, _links in listed:
        path = history.release_history_path(release.tag)
        detail = client.get(path)
        assert detail.status_code == 200
        canonical = _canonical(detail.get_data(as_text=True))
        assert canonical in locations
        assert canonical.endswith(path)
        assert f'href="{path}"' in index


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
    # The request's single Git budget, less what the cache lookup took.
    assert 0 < calls[0][1]["timeout"] <= history.GIT_TIMEOUT_SEC
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
        lambda release, **kwargs: pytest.fail("Git cache must survive link refresh"),
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

        def checked(release, **kwargs):
            assert not session.in_transaction()
            return real(release, **kwargs)

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

    def counted(release, **kwargs):
        calls.append(release.tag)
        return calculate(release, **kwargs)

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

    def unexpected(release, **kwargs):
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

    def timeout(*args, **kwargs):
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


def test_concurrent_cold_requests_for_one_release_walk_git_once(
    client, releases, seeded_db, monkeypatch
):
    import threading

    from mimir import release_history as history

    monkeypatch.setattr(history, "_linus_path", lambda: releases[0])
    _record(seeded_db, releases[2], "art3@example.com")
    release = history.release_catalog()[0]
    assert history.cached_facts([release]) == {}, "the release must start cold"
    calculate = history.calculate_git
    calls = []
    walking, finish = threading.Event(), threading.Event()

    def slow(release, **kwargs):
        calls.append(release.tag)
        walking.set()
        # Hold the first walk open until the second request is waiting
        # on it, so the two genuinely overlap.
        assert finish.wait(10), "test failed to release the walk"
        return calculate(release, **kwargs)

    monkeypatch.setattr(history, "calculate_git", slow)
    responses = {}

    def fetch(name):
        responses[name] = client.application.test_client().get(
            history.release_history_path(release.tag)
        )

    first = threading.Thread(target=fetch, args=("first",))
    first.start()
    assert walking.wait(10)
    second = threading.Thread(target=fetch, args=("second",))
    second.start()
    # The second request is blocked on the walk, not finished.
    second.join(0.5)
    assert second.is_alive()
    finish.set()
    first.join(10)
    second.join(10)
    assert calls == [release.tag]
    assert sorted(responses) == ["first", "second"]
    for response in responses.values():
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Each 200 carries the release's data, never a placeholder.
        assert "1 recorded mailing-list references" in body
        assert "cross-posted note" in body


def test_single_flight_excludes_another_process(tmp_path, monkeypatch):
    import subprocess
    import sys
    import tempfile
    import time

    from mimir import release_history as history

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    key = "release_history:1:test"
    path = tmp_path / ("mimir-" + key.replace(":", "-") + ".lock")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys\n"
            f"fd = os.open({str(path)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.lockf(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "sys.stdin.read()\n",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            with history._single_flight(key, time.monotonic() + 0.3):
                pytest.fail("acquired a lock another process holds")
        assert time.monotonic() - started >= 0.3
    finally:
        holder.stdin.close()
        holder.wait(10)
    # Released by the holder's exit, so the next caller gets it at once.
    with history._single_flight(key, time.monotonic() + 1):
        pass


def test_sitemap_and_index_list_only_releases_with_cached_recorded_history(
    client, releases, seeded_db, monkeypatch
):
    from dulwich.objects import Tree

    from mimir import release_history as history

    path, baseline, patch, rc, final = releases
    with Repo(str(path)) as repo:
        tree = Tree()
        repo.object_store.add_object(tree)
        repo.refs[b"refs/tags/v2.6.11"] = tree.id
        later = add_commit(repo, b"Linus <linus@x>", [final], b"7.1", when=500000).id
        repo.refs[b"refs/tags/v7.1"] = later
        last = add_commit(repo, b"Linus <linus@x>", [later], b"7.2", when=600000).id
        repo.refs[b"refs/tags/v7.2"] = last
    monkeypatch.setattr(history, "_linus_path", lambda: path)
    _record(seeded_db, patch, "art3@example.com")
    # v7.2 has a reference too, so only its uncached state can keep it out.
    _record(seeded_db, last, "art4@example.com")
    by_tag = {r.tag: r for r in history.release_catalog()}
    assert sorted(by_tag) == ["v2.6.11", "v6.19", "v7.0", "v7.1", "v7.2"]
    for tag in ("v2.6.11", "v6.19", "v7.0", "v7.1"):
        history.release_data(by_tag[tag])
    # v7.1 has no reference, so it is decided once its pull requests are
    # classified too (#1135): it has none.
    with seeded_db() as s:
        history.warm_release_pulls(s, history.release_catalog())
    assert history.cache.get(by_tag["v7.1"].key + history.PULLS)["count"] == 0
    # Preconditions: each excluded release is excluded for its own reason.
    facts, links = (
        history.cached_facts(by_tag.values()),
        history.cached_links(by_tag.values()),
    )
    assert facts["v2.6.11"].commit_count is None
    assert links["v7.1"].pair_count == 0 and facts["v7.1"].commit_count == 1
    assert links["v7.0"].pair_count == 1
    assert "v7.2" not in links

    def unexpected(*args, **kwargs):
        pytest.fail("deciding what to list must never walk Git")

    monkeypatch.setattr(history, "calculate_git", unexpected)
    prefix = history.release_history_path() + "/"

    def sitemap_tags():
        return {
            loc.rsplit("/", 1)[1] for loc in _sitemap_locations(client) if prefix in loc
        }

    def index():
        return client.get(history.release_history_path()).get_data(as_text=True)

    def index_tags(body):
        return set(re.findall(re.escape(prefix) + r'(v[\d.]+)"', body))

    assert sitemap_tags() == {"v7.0"}
    body = index()
    assert index_tags(body) == {"v7.0"}
    assert "Not calculated yet" not in body
    assert re.search(r"\b(\d+) of (\d+) checked releases", body).groups() == (
        "1",
        "4",
    )
    assert re.search(r"\b(\d+) more release has not been checked", body)[1] == "1"

    # The warm fills v7.2 in: the cached sitemap must not hold it out.
    monkeypatch.undo()
    monkeypatch.setattr(history, "_linus_path", lambda: path)
    history.release_data(by_tag["v7.2"])
    monkeypatch.setattr(history, "calculate_git", unexpected)
    assert sitemap_tags() == {"v7.0", "v7.2"}
    body = index()
    assert index_tags(body) == {"v7.0", "v7.2"}
    assert re.search(r"\b(\d+) of (\d+) checked releases", body).groups() == (
        "2",
        "5",
    )
    assert "not been checked" not in body
    # Releases without references stay reachable by URL.
    assert client.get(history.release_history_path("v7.1")).status_code == 200


def test_archive_total_counts_only_messages_that_get_a_url(seeded_db, monkeypatch):
    from sqlalchemy import select, update

    from mimir import release_history as history
    from mimir.models import Article, ArticleList, Inbox

    mids = [f"art{n}@example.com" for n in range(1, 5)]
    with seeded_db() as session:
        art2 = session.scalar(select(Article).where(Article.message_id == mids[1]))
        # The two fallbacks `_advertised_urls_for` takes: a canonical inbox
        # the article is not linked in, and a root not yet computed.
        art2.canonical_inbox_id = session.scalar(
            select(Inbox.id).where(Inbox.name == "alpha")
        )
        session.execute(
            update(ArticleList)
            .where(ArticleList.article_id == art2.id)
            .values(thread_root_id=None)
        )
        session.commit()
    for size in (1, 3, 50):
        monkeypatch.setattr(history, "PAGE_SIZE", size)
        with seeded_db() as session:
            shown = 0
            page = 1
            while True:
                rows, total = history.archived_page(session, mids, page)
                if not rows:
                    break
                assert len(rows) == min(size, total - (page - 1) * size)
                shown += len(rows)
                page += 1
            assert total == 4
            assert shown == total, size


def test_warm_renews_git_facts_so_listed_releases_never_lapse(
    releases, seeded_db, monkeypatch
):
    import random

    from mimir import cache
    from mimir import release_history as history

    monkeypatch.setattr(history, "_linus_path", lambda: releases[0])
    _record(seeded_db, releases[2], "art3@example.com")
    release = history.release_catalog()[0]
    now = cache._now()
    clock = [now]
    monkeypatch.setattr(cache, "_now", lambda: clock[0])
    window = 7200
    with cache.warm_window(window):
        history.release_data(release)

    def listed_tags():
        catalog = history.release_catalog()
        return [r.tag for r, _f, _l in history.recorded_history(catalog).listed]

    assert listed_tags() == ["v7.0"]

    def unexpected(*args, **kwargs):
        pytest.fail("renewing immutable Git facts must not walk Git")

    monkeypatch.setattr(history, "calculate_git", unexpected)
    # Refresh as early as the window allows, as warm runs do by chance.
    # Links then refresh about 22 h apart, drifting against the 30-day
    # facts expiry the way production's do, so a renewal tied to the
    # links refresh misses the facts' window; one checked every pass
    # cannot.
    monkeypatch.setattr(random, "random", lambda: 0.0)
    links_refreshes = 0
    last_links = history.cached_links([release])[release.tag].computed_at
    hours = (history.GIT_TTL_SEC + 3 * window) // 3600
    for hour in range(1, hours + 1):
        clock[0] = now + hour * 3600
        with cache.warm_window(window), seeded_db() as session:
            history.warm_release(session, release)
        assert listed_tags() == ["v7.0"], f"left the listing at hour {hour}"
        links = history.cached_links([release])[release.tag].computed_at
        links_refreshes += links != last_links
        last_links = links
    # Precondition: the walk crossed the original facts expiry with links
    # kept fresh by their own daily cadence, not recomputed on demand.
    assert hours * 3600 > history.GIT_TTL_SEC + window
    assert 30 <= links_refreshes <= hours // 20


def _seed_references(seeded_db, releases, monkeypatch, sizes, warm=True):
    """Record `{subsystem: n}` single-patch references in v7.0's range:
    one archived article per reference, inside a directory named after its
    subsystem. Git is faked to a range of exactly those commits, so the
    test controls the count and no walk is needed. Returns the release."""
    from mimir import release_history as history
    from mimir.release_history import ReleaseFacts
    from tests.test_merge_window import _article, _commit, _subsystem

    shas = []
    with seeded_db() as s:
        for name, count in sizes.items():
            directory = name.split()[0].lower()
            _subsystem(s, name, [f"{directory}/"])
            for n in range(count):
                mid = f"{directory}{n}@x"
                _article(s, mid, days=n, files=[f"{directory}/{n}.c"])
                shas.append(f"{len(shas):040x}")
                _commit(s, mid, "linus", sha=shas[-1])
        s.commit()
    facts = ReleaseFacts(
        release_date=None,
        date_source=None,
        merge_days=None,
        stabilization_days=None,
        rc_count=0,
        commit_count=len(shas),
    )
    monkeypatch.setattr(history, "_linus_path", lambda: releases[0])
    monkeypatch.setattr(history, "calculate_git", lambda r, timeout=0: (facts, shas))
    release = history.release_catalog(releases[0])[0]
    if warm:
        # What the hourly warm does: the links, and the grouping with them.
        history.release_data(release, group=True)
    return release


def _history_page(client, tag="v7.0"):
    from mimir import release_history as history
    from tests._dom import parse

    return parse(client.get(history.release_history_path(tag)).get_data(as_text=True))


def test_detail_groups_references_by_subsystem_with_bars_in_the_release(
    client, releases, seeded_db, monkeypatch
):
    import mimir.merge_window as mwmod
    from mimir import release_history as history
    from tests._dom import parse

    monkeypatch.setattr(mwmod, "SECTION_MIN_PATCHES", 3)
    _seed_references(
        seeded_db, releases, monkeypatch, {"ALPHA SUB": 8, "BETA": 3, "TINY": 1}
    )
    page = _history_page(client)
    listing = page.find("ul", id="mw-subsystems")
    rows = [c for c in listing.children if getattr(c, "tag", None) == "li"]
    names = [r.find_all("span", cls="mw-name")[0].text() for r in rows]
    assert names == ["alpha sub", "beta", "1 smaller subsystem"]
    assert [r.find_all("span", cls="count")[0].text() for r in rows] == ["8", "3", "1"]
    # Bars scale to the release's largest subsystem.
    assert [r.find_all("rect")[0].attrs["width"] for r in rows[:2]] == [
        "100.0",
        "37.5",
    ]
    # The references are listed below, paginated; the groups do not repeat
    # them, so a row has no disclosure of its own to open.
    assert rows[0].find_all("details") == []
    assert rows[2].find_all("details")[0].attrs["id"] == "smaller-subsystems"
    assert "of this release's recorded references" in page.text()
    # Page 2 lists more messages, not the table again.
    monkeypatch.setattr(history, "PAGE_SIZE", 5)
    assert _history_page(client).find_all("ul", id="mw-subsystems") != []
    second = parse(
        client.get(history.release_history_path("v7.0", page=2)).get_data(as_text=True)
    )
    assert second.find_all("ul", id="mw-subsystems") == []
    assert second.find_all("ul", cls="divided") != []


def test_detail_with_a_handful_of_references_gets_a_note_not_a_table(
    client, releases, seeded_db, monkeypatch
):
    _seed_references(seeded_db, releases, monkeypatch, {"ALPHA SUB": 3})
    page = _history_page(client)
    assert page.find_all("ul", id="mw-subsystems") == []
    assert "Only 3 archived references, too few for a subsystem breakdown." in (
        page.text()
    )


def test_subsystem_groups_are_cached_with_the_links_and_read_back(
    client, releases, seeded_db, monkeypatch
):
    """Written under `<release key>:subsystems` by the warm's links
    refresh, and only read by the page: a hit never groups, and a lost
    entry means no section until the warm fills it again."""
    from mimir import cache
    from mimir import release_history as history

    monkeypatch.setattr(history, "HANDFUL_OF_REFERENCES", 1)
    release = _seed_references(seeded_db, releases, monkeypatch, {"ALPHA SUB": 4})
    key = release.key + ":subsystems"
    assert cache.get(key) is not None
    monkeypatch.setattr(
        history, "calculate_subsystems", lambda *a: pytest.fail("a request grouped")
    )
    assert "alpha sub" in _history_page(client).find("ul", id="mw-subsystems").text()
    cache.delete(key)
    assert _history_page(client).find_all("ul", id="mw-subsystems") == []


def test_subsystem_groups_refresh_with_the_daily_links_refresh(
    releases, seeded_db, monkeypatch
):
    from mimir import cache
    from mimir import release_history as history

    release = _seed_references(seeded_db, releases, monkeypatch, {"ALPHA SUB": 4})
    now = cache._now()
    history.release_data(release, group=True)
    first = cache.get(release.key + ":subsystems")
    with seeded_db() as s:
        from tests.test_merge_window import _article, _commit

        _article(s, "alpha-late@x", files=["alpha/late.c"])
        _commit(s, "alpha-late@x", "linus", sha="f" * 40)
        s.commit()
    monkeypatch.setattr(cache, "_now", lambda: now + history.LINKS_TTL_SEC + 2)
    monkeypatch.setattr(
        history,
        "git_facts",
        lambda r: (
            history.ReleaseFacts.model_validate(cache.get(release.key + ":facts")),
            [f"{n:040x}" for n in range(4)] + ["f" * 40],
        ),
    )
    history.release_data(release, group=True)
    assert first["references"] == 4
    assert cache.get(release.key + ":subsystems")["references"] == 5


def test_subsystem_references_are_the_messages_the_page_lists(seeded_db):
    """The table counts what `archived_page` can reach: an undated message
    or one on no list is not a reference here either."""
    from mimir import release_history as history
    from mimir.models import Article
    from tests.test_merge_window import _article

    with seeded_db() as s:
        _article(s, "ok@x", files=["a/b.c"])
        _article(s, "undated@x", files=["a/b.c"]).date = None
        s.add(Article(message_id="nolist@x", subject="s", date=datetime.now(UTC)))
        s.commit()
        ids = ["ok@x", "undated@x", "nolist@x", "absent@x"]
        assert history.archived_page(s, ids, 1)[1] == 1
        assert history.calculate_subsystems(s, ids).references == 1


def test_detail_with_no_archived_references_shows_no_subsystem_note(
    client, releases, seeded_db, monkeypatch
):
    """A release with nothing archived already says so under the message list; a "By subsystem" heading over "Only 0 archived references" adds a second, odd sentence for the same fact."""
    _seed_references(seeded_db, releases, monkeypatch, {})
    page = _history_page(client)
    assert "No archived messages are available for this range." in page.text()
    assert page.find_all("h2", id="subsystems") == []
    assert "Only 0 archived" not in page.text()


def test_the_message_list_has_its_own_heading_under_the_subsystem_one(
    client, releases, seeded_db, monkeypatch
):
    """Without it the messages sit under "By subsystem" for anyone
    navigating by heading."""
    from mimir import release_history as history
    from tests._dom import parse

    _seed_references(seeded_db, releases, monkeypatch, {"ALPHA SUB": 12})
    monkeypatch.setattr(history, "PAGE_SIZE", 5)
    page = _history_page(client)
    assert [h.attrs["id"] for h in page.find_all("h2") if "id" in h.attrs] == [
        "subsystems",
        "messages",
    ]
    assert page.find("h2", id="messages").text() == "Archived messages"
    second = parse(
        client.get(history.release_history_path("v7.0", page=2)).get_data(as_text=True)
    )
    assert second.find_all("h2", id="subsystems") == []
    assert second.find_all("h2", id="messages") == []


@pytest.mark.parametrize(("count", "table"), [(9, False), (10, True)])
def test_the_handful_boundary_is_ten_references(
    client, releases, seeded_db, monkeypatch, count, table
):
    from mimir import release_history as history

    assert history.HANDFUL_OF_REFERENCES == 10
    _seed_references(seeded_db, releases, monkeypatch, {"ALPHA SUB": count})
    page = _history_page(client)
    assert (page.find_all("ul", id="mw-subsystems") != []) is table
    assert ("too few for a subsystem breakdown" in page.text()) is (not table)


def test_a_page_view_on_a_cache_miss_never_groups_and_shows_no_section(
    client, releases, seeded_db, monkeypatch
):
    """The grouping is filled by the warm pass, never by a request: a miss
    renders no table, no note and no "Archived messages" heading, and the
    cold links path of the request does not group either."""
    from mimir import cache
    from mimir import release_history as history

    release = _seed_references(
        seeded_db, releases, monkeypatch, {"ALPHA SUB": 12}, warm=False
    )
    calls = []
    monkeypatch.setattr(
        history, "calculate_subsystems", lambda *a: calls.append(a) or pytest.fail()
    )
    page = _history_page(client)
    assert page.find_all("h2", id="subsystems") == []
    assert page.find_all("h2", id="messages") == []
    assert page.find_all("ul", id="mw-subsystems") == []
    assert "subsystem breakdown" not in page.text()
    # The messages are still listed: only the grouping is missing.
    assert page.find_all("ul", cls="divided") != []
    assert calls == []
    assert cache.get(release.key + ":subsystems") is None


def _fake_releases(n):
    """Releases newest first, with cached ID lists, as `release_catalog`
    returns them. Their keys are made up: only the cache rows matter."""
    from mimir import cache
    from mimir import release_history as history

    out = []
    for i in range(n):
        release = history.Release(
            f"v9.{n - i}", None, "0" * 40, None, None, (), f"fake:{n - i}"
        )
        cache.set(
            release.key + ":links_data",
            {"summary": {}, "message_ids": [f"m{n - i}@x"]},
            history.LINKS_TTL_SEC,
        )
        out.append(release)
    return out


def _clocked_calculate(monkeypatch, step):
    """Replace the grouping by a stub that costs `step` seconds on a fake
    clock, and return the list of the releases it was asked for."""
    from mimir import release_history as history

    now = [0.0]
    asked = []
    monkeypatch.setattr(history.time, "monotonic", lambda: now[0])

    def stub(session, message_ids):
        now[0] += step
        asked.append(message_ids[0])
        return history.ReleaseSubsystems(
            sections=[], smaller=[], smaller_patches=0, references=1
        )

    monkeypatch.setattr(history, "calculate_subsystems", stub)
    return asked


def test_the_warm_fills_missing_groupings_newest_first_within_its_budget(
    seeded_db, monkeypatch
):
    from mimir import cache
    from mimir import release_history as history

    releases = _fake_releases(4)
    asked = _clocked_calculate(monkeypatch, step=10)
    with seeded_db() as session:
        history.warm_release_subsystems(session, releases, budget_sec=15)
    # Newest first; the budget is checked before each one, so a release
    # that starts in time finishes (10 s, then 20 s is past 15 s).
    assert asked == ["m4@x", "m3@x"]
    assert [bool(cache.get(r.key + ":subsystems")) for r in releases] == [
        True,
        True,
        False,
        False,
    ]


def test_a_later_warm_pass_picks_up_the_rest_and_renews_nothing(seeded_db, monkeypatch):
    from mimir import cache
    from mimir import release_history as history

    releases = _fake_releases(4)
    asked = _clocked_calculate(monkeypatch, step=10)
    with seeded_db() as session:
        history.warm_release_subsystems(session, releases, budget_sec=15)
        asked.clear()
        history.warm_release_subsystems(session, releases, budget_sec=15)
        assert asked == ["m2@x", "m1@x"]
        asked.clear()
        history.warm_release_subsystems(session, releases, budget_sec=15)
        assert asked == []
    assert all(cache.get(r.key + ":subsystems") for r in releases)


def test_the_warm_skips_a_release_whose_links_are_not_cached_yet(
    seeded_db, monkeypatch
):
    from mimir import cache
    from mimir import release_history as history

    releases = _fake_releases(2)
    cache.delete(releases[0].key + ":links_data")
    asked = _clocked_calculate(monkeypatch, step=1)
    with seeded_db() as session:
        history.warm_release_subsystems(session, releases, budget_sec=100)
    assert asked == ["m1@x"]


def test_a_release_whose_grouping_fails_does_not_block_the_older_ones(
    seeded_db, monkeypatch
):
    from mimir import cache
    from mimir import release_history as history

    releases = _fake_releases(3)
    asked = []

    def stub(session, message_ids):
        asked.append(message_ids[0])
        if message_ids[0] == "m3@x":
            raise RuntimeError("boom")
        return history.ReleaseSubsystems(
            sections=[], smaller=[], smaller_patches=0, references=1
        )

    monkeypatch.setattr(history, "calculate_subsystems", stub)
    with seeded_db() as session:
        history.warm_release_subsystems(session, releases, budget_sec=100)
    assert asked == ["m3@x", "m2@x", "m1@x"]
    assert [bool(cache.get(r.key + ":subsystems")) for r in releases] == [
        False,
        True,
        True,
    ]


def test_the_warm_links_refresh_writes_the_grouping(releases, seeded_db, monkeypatch):
    """The daily links refresh in the warm rewrites the grouping with the links, so a row the hourly fill wrote is renewed before it expires and the section never drops out for an hour."""
    from mimir import cache
    from mimir import release_history as history

    release = _seed_references(
        seeded_db, releases, monkeypatch, {"ALPHA SUB": 4}, warm=False
    )
    with seeded_db() as session:
        history.warm_release(session, release)
    assert cache.get(release.key + ":subsystems")["references"] == 4
