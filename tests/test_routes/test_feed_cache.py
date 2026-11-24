"""Feed link caching preserves a bounded snapshot without warm rank reads."""

from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from sqlalchemy import delete, event, select

from mimir import cache
from mimir.cli.cache import _build_fast_inbox_targets, _build_slow_inbox_targets
from mimir.config import settings
from mimir.dashboard import LISTING_CACHE_TTL_SEC, ArticleSummary, feed_articles
from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, CacheEntry, Inbox
from mimir.web import urls
from tests.test_routes._helpers import build_thread
from tests.test_routes.test_feed_window import entries, link, seed_dates


@pytest.mark.parametrize("suffix", ["", "/author/b.example"])
@freeze_time("2024-01-02 12:00:00")
def test_warm_feed_reads_no_canonical_or_rank_queries(
    client, tmp_path, monkeypatch, suffix
):
    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = build_thread(tmp_path, "alpha", size=7, cross_post_to="beta")
    path = f"/beta{suffix}/feed.atom"
    first = entries(client.get(path))
    aid = seeded["m6"][0]
    assert link(first[aid]).endswith(f"/t/4#m{aid}")

    def forbidden(*args, **kwargs):
        pytest.fail("a warm feed resolved canonical destinations")

    monkeypatch.setattr(urls, "_canonical_destinations_for", forbidden)
    with SessionLocal() as session:
        engine = session.get_bind()
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        second = entries(client.get(path))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert list(second) == list(first)
    assert link(second[aid]) == link(first[aid])
    assert not any(
        "canonical_root" in sql or "WITH wanted" in sql for sql in statements
    ), statements
    assert len([k for k in cache.keys() if k.startswith("feed_links:v1:beta:")]) == 1


@pytest.mark.parametrize("window", [600, 7200])
@pytest.mark.parametrize("author", ["", "feed@example"])
def test_feed_warm_targets_store_links_for_only_listing_ttl(
    client, frozen_clock, window, author
):
    seed_dates([datetime.now(UTC)])
    with SessionLocal() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        inbox.tracked_authors = {"Feed": "feed@example"}
        today = datetime.now(UTC).date()
        targets = (
            _build_slow_inbox_targets(inbox, today, today - timedelta(days=1))
            if author
            else _build_fast_inbox_targets(inbox)
        )
        target = next(
            fn
            for label, fn in targets
            if "tracker:Feed (feed)" in label
            or (not author and "feed_articles" in label)
        )
        with cache.warm_window(window):
            target(session)
            cache.set("feed_test_after_scope", [], 10)
    keys = [k for k in cache.keys() if k.startswith("feed_links:v1:alpha:")]
    assert len(keys) == 1
    with SessionLocal() as session:
        row = session.scalar(
            select(CacheEntry).where(CacheEntry.key == cache._ns(keys[0]))
        )
        after = session.scalar(
            select(CacheEntry).where(
                CacheEntry.key == cache._ns("feed_test_after_scope")
            )
        )
        assert row.expires_at - cache._now() == LISTING_CACHE_TTL_SEC
        assert after.expires_at - cache._now() == 10 + window
    path = f"/alpha/author/{author}/feed.atom" if author else "/alpha/feed.atom"
    assert len(entries(client.get(path))) == 1


@pytest.mark.parametrize("suffix", ["", "/author/feed@example"])
def test_age_out_reuses_cached_destination_superset(
    client, frozen_clock, monkeypatch, suffix
):
    floor = datetime.now(UTC) - timedelta(days=5)
    ids = seed_dates([floor, floor + timedelta(seconds=2)])
    path = f"/alpha{suffix}/feed.atom"
    assert list(entries(client.get(path))) == ids[::-1]
    key = next(k for k in cache.keys() if k.startswith("feed_links:v1:alpha:"))
    deadline = cache.get(key)["expires_at"]
    frozen_clock.tick(timedelta(seconds=1))

    def forbidden(*args, **kwargs):
        pytest.fail("aging out a cached entry recomputed its surviving links")

    monkeypatch.setattr(urls, "_canonical_destinations_for", forbidden)
    assert list(entries(client.get(path))) == [ids[1]]

    assert cache.get(key)["expires_at"] == deadline


@pytest.mark.parametrize("suffix", ["", "/author/feed@example"])
def test_refreshed_listing_never_uses_a_map_for_different_entries(
    client, frozen_clock, suffix
):
    ids = seed_dates([datetime.now(UTC) - timedelta(seconds=1)])
    path = f"/alpha{suffix}/feed.atom"
    assert list(entries(client.get(path))) == ids
    with SessionLocal() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        article = Article(
            message_id="new-feed-entry@example",
            author="Feed Author <feed@example>",
            date=datetime.now(UTC),
        )
        session.add(article)
        session.flush()
        session.add(
            ArticleList(
                article_id=article.id,
                inbox_id=inbox.id,
                epoch="0.git",
                commit_sha="f" * 40,
                thread_root_id=article.id,
            )
        )
        session.commit()
        new = ArticleSummary(
            id=article.id,
            subject=article.subject,
            author=article.author,
            date=article.date,
        )
    author = "feed@example" if suffix else ""
    cache.set(f"feed_articles:alpha:5:1000:{author}", [new], LISTING_CACHE_TTL_SEC)
    result = entries(client.get(path))
    assert list(result) == [new.id]
    assert link(result[new.id]).endswith(f"/{new.id}/t#m{new.id}")


def test_feed_link_caches_isolate_inboxes_and_authors(client, frozen_clock):
    aid = seed_dates([datetime.now(UTC)])[0]
    assert list(entries(client.get("/alpha/feed.atom"))) == [aid]
    assert list(entries(client.get("/alpha/author/feed@example/feed.atom"))) == [aid]
    assert not entries(client.get("/alpha/author/nobody/feed.atom"))
    assert not entries(client.get("/beta/feed.atom"))
    assert len([k for k in cache.keys() if k.startswith("feed_links:v1:")]) == 4


def test_same_id_with_refreshed_date_replaces_its_cached_path(client, frozen_clock):
    frozen_clock.move_to("2024-07-02 12:00:00")
    aid = seed_dates([datetime.now(UTC) - timedelta(days=2)])[0]
    path = "/alpha/feed.atom"
    first = entries(client.get(path))[aid]
    assert link(first).endswith(f"/alpha/2024/06/{aid}/t#m{aid}")
    with SessionLocal() as session:
        article = session.get(Article, aid)
        article.date = datetime.now(UTC) - timedelta(days=1)
        session.commit()
        changed = ArticleSummary(
            id=aid, subject=article.subject, author=article.author, date=article.date
        )
    cache.set("feed_articles:alpha:5:1000:", [changed], LISTING_CACHE_TTL_SEC)
    second = entries(client.get(path))[aid]
    assert link(second).endswith(f"/alpha/2024/07/{aid}/t#m{aid}")
    assert link(second) != link(first)


def test_unavailable_destination_is_cached_without_repeated_resolution(
    client, frozen_clock, monkeypatch
):
    aid = seed_dates([datetime.now(UTC)])[0]
    with SessionLocal() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        assert [a.id for a in feed_articles(session, inbox)] == [aid]
        session.execute(delete(ArticleList).where(ArticleList.article_id == aid))
        session.commit()
    assert cache.get("feed_articles:alpha:5:1000:") is not None
    path = "/alpha/feed.atom"
    assert not entries(client.get(path))
    key = next(k for k in cache.keys() if k.startswith("feed_links:v1:alpha:"))
    saved = cache.get(key)
    assert [row[0] for row in saved["entries"]] == [aid]
    assert saved["destinations"] == []

    def forbidden(*args, **kwargs):
        pytest.fail("an unavailable destination was resolved again inside its TTL")

    monkeypatch.setattr(urls, "_canonical_destinations_for", forbidden)
    assert not entries(client.get(path))


def test_canonical_policy_change_refreshes_warm_link_and_tag_inbox(
    client, frozen_clock, monkeypatch
):
    monkeypatch.setattr(settings, "canonical_demoted_inboxes", [])
    aid = seed_dates([datetime.now(UTC)])[0]
    with SessionLocal() as session:
        beta = session.scalar(select(Inbox).where(Inbox.name == "beta"))
        article = session.get(Article, aid)
        assert article.canonical_inbox_id is None
        session.add(
            ArticleList(
                article_id=aid,
                inbox_id=beta.id,
                epoch="0.git",
                commit_sha="e" * 40,
                thread_root_id=aid,
            )
        )
        session.commit()
    path = "/alpha/feed.atom"
    first = entries(client.get(path))[aid]
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    assert (
        first.findtext("{http://www.w3.org/2005/Atom}id")
        == f"tag:mimir,{date}:alpha/{aid}"
    )
    assert link(first).endswith(f"/alpha/2024/06/{aid}/t#m{aid}")
    assert cache.get("feed_articles:alpha:5:1000:") is not None
    assert len([k for k in cache.keys() if k.startswith("feed_links:v1:alpha:")]) == 1
    monkeypatch.setattr(settings, "canonical_demoted_inboxes", ["alpha"])
    second = entries(client.get(path))[aid]
    assert (
        second.findtext("{http://www.w3.org/2005/Atom}id")
        == f"tag:mimir,{date}:beta/{aid}"
    )
    assert link(second).endswith(f"/beta/2024/06/{aid}/t#m{aid}")
    assert len([k for k in cache.keys() if k.startswith("feed_links:v1:alpha:")]) == 2


@pytest.mark.parametrize("suffix", ["", "/author/feed@example"])
def test_http_cache_uses_only_remaining_link_lifetime(
    client, frozen_clock, monkeypatch, suffix
):
    seed_dates([datetime.now(UTC)])
    path = f"/alpha{suffix}/feed.atom"
    with cache.warm_window(7200):
        first = client.get(path)
    assert len(entries(first)) == 1
    calls = []
    resolve = urls._canonical_destinations_for

    def counted(*args, **kwargs):
        calls.append(1)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(urls, "_canonical_destinations_for", counted)
    frozen_clock.tick(timedelta(seconds=299))
    near = client.get(path)
    assert len(entries(near)) == 1
    assert near.cache_control.public
    assert 0 <= near.cache_control.max_age <= 1
    assert calls == []
    frozen_clock.tick(timedelta(seconds=1))
    expired = client.get(path)
    assert len(entries(expired)) == 1
    assert len(calls) == 1
    assert expired.cache_control.max_age == LISTING_CACHE_TTL_SEC
    assert expired.headers["ETag"] == first.headers["ETag"]
    assert "Last-Modified" not in expired.headers


@pytest.mark.parametrize("suffix", ["", "/author/feed@example"])
@pytest.mark.parametrize("elapsed", [300, 301])
def test_embedded_expiry_wins_over_a_longer_lived_cache_row(
    client, frozen_clock, monkeypatch, suffix, elapsed
):
    seed_dates([datetime.now(UTC)])
    path = f"/alpha{suffix}/feed.atom"
    with cache.warm_window(7200):
        assert len(entries(client.get(path))) == 1
    key = next(k for k in cache.keys() if k.startswith("feed_links:v1:alpha:"))
    saved = cache.get(key)
    cache.set(key, saved, 1000)
    frozen_clock.tick(timedelta(seconds=elapsed))
    assert cache.get(key) is not None
    author = "feed@example" if suffix else ""
    assert cache.get(f"feed_articles:alpha:5:1000:{author}") is not None
    calls = []
    resolve = urls._canonical_destinations_for

    def counted(*args, **kwargs):
        calls.append(1)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(urls, "_canonical_destinations_for", counted)
    assert len(entries(client.get(path))) == 1
    assert len(calls) == 1
    assert cache.get(key)["expires_at"] == cache._now() + LISTING_CACHE_TTL_SEC


def test_resolving_and_rendering_consume_the_same_freshness_budget(
    client, frozen_clock, monkeypatch
):
    import mimir.seo.atom as atom

    seed_dates([datetime.now(UTC)])
    resolve = urls._canonical_destinations_for
    render = atom.tostring
    started = cache._now()

    def slow_resolve(*args, **kwargs):
        frozen_clock.tick(timedelta(seconds=3))
        return resolve(*args, **kwargs)

    def slow_render(*args, **kwargs):
        body = render(*args, **kwargs)
        frozen_clock.tick(timedelta(seconds=2))
        return body

    monkeypatch.setattr(urls, "_canonical_destinations_for", slow_resolve)
    monkeypatch.setattr(atom, "tostring", slow_render)
    response = client.get("/alpha/feed.atom")
    assert len(entries(response)) == 1
    assert response.cache_control.max_age == LISTING_CACHE_TTL_SEC - 5
    key = next(k for k in cache.keys() if k.startswith("feed_links:v1:alpha:"))
    assert cache.get(key)["expires_at"] == started + LISTING_CACHE_TTL_SEC
