"""Feed URLs follow real message canonicals, with a bounded rolling window."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import pytest
from freezegun import freeze_time
from sqlalchemy import delete, event, select

from mimir import cache
from mimir.config import settings
from mimir.dashboard import (
    LISTING_CACHE_TTL_SEC,
    RECENT_INBOX_FIRST_BELOW_ARTICLES,
    feed_articles,
)
from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, Inbox
from mimir.web.urls import _canonical_destinations_for
from tests._helpers import PRODUCTION_STATS, _cache_total, plan_of, planner_stats
from tests.test_routes._helpers import build_thread

NS = {"a": "http://www.w3.org/2005/Atom"}


def entries(response):
    assert response.status_code == 200
    return {
        int(entry.findtext("a:id", namespaces=NS).rsplit("/", 1)[1]): entry
        for entry in ET.fromstring(response.data).findall("a:entry", NS)
    }


def link(entry):
    return entry.find("a:link[@rel='alternate']", NS).attrib["href"]


@pytest.mark.parametrize("author", [False, True])
@pytest.mark.parametrize("unroot", [(), (2,)])
@freeze_time("2024-01-02 12:00:00")
def test_feed_link_matches_message_canonical_and_real_anchor(
    client, tmp_path, monkeypatch, author, unroot
):
    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = build_thread(
        tmp_path, "alpha", size=7, dates="ties", cross_post_to="beta", unroot=unroot
    )
    suffix = "/author/b.example" if author else ""
    feed = entries(client.get(f"/beta{suffix}/feed.atom"))
    checked = 0
    for article_id, message_url in seeded.values():
        entry = feed[article_id]
        response = client.get(message_url)
        assert response.status_code == 200
        canonical = re.search(
            r'<link rel="canonical" href="([^"]+)"', response.get_data(as_text=True)
        ).group(1)
        assert link(entry) == f"{canonical}#m{article_id}"
        target = client.get(urlsplit(canonical).path)
        assert target.status_code == 200
        assert f'id="m{article_id}"' in target.get_data(as_text=True)
        assert entry.findtext("a:id", namespaces=NS) == (
            f"tag:mimir,2024-01-01:alpha/{article_id}"
        )
        if not unroot and article_id == seeded["m6"][0]:
            assert canonical.endswith("/t/4")
        checked += 1
    assert checked == 7


@pytest.mark.parametrize("change_cap", [False, True])
def test_warm_feed_tracks_root_and_page_changes(
    client, tmp_path, monkeypatch, frozen_clock, change_cap
):
    frozen_clock.move_to("2024-01-02 12:00:00")
    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = build_thread(tmp_path, "alpha", size=7)
    aid, message_url = seeded["m6"]
    assert client.get(message_url).status_code == 200
    with cache.warm_window(7200):
        first_response = client.get("/alpha/feed.atom")
    first = entries(first_response)[aid]
    assert cache.get("feed_articles:alpha:5:1000:") is not None
    assert link(first).endswith(f"/t/4#m{aid}")
    with SessionLocal() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        old_root = session.get(Article, seeded["m0"][0])
        new_root = session.get(Article, seeded["m1"][0])
        new_root.thread_parent = None
        old_root.thread_parent = new_root.message_id
        for member in session.scalars(
            select(ArticleList).where(ArticleList.inbox_id == inbox.id)
        ):
            if member.article_id in {row[0] for row in seeded.values()}:
                member.thread_root_id = new_root.id
        session.commit()
    if change_cap:
        monkeypatch.setattr(settings, "thread_view_render_cap", 3)
    else:
        assert link(entries(client.get("/alpha/feed.atom"))[aid]) == link(first)
        frozen_clock.tick(timedelta(seconds=LISTING_CACHE_TTL_SEC + 1))
        assert cache.get("feed_articles:alpha:5:1000:") is not None
    # Leave listing and message caches warm. There are no feed validators.
    # conditional headers must not hide the changed destination behind 304.
    response = client.get(
        "/alpha/feed.atom",
        headers={
            "If-None-Match": '"old"',
            "If-Modified-Since": "Tue, 02 Jan 2024 12:00:00 GMT",
        },
    )
    second = entries(response)[aid]
    assert link(second) != link(first)
    page = 3 if change_cap else 4
    assert link(second).endswith(f"/{seeded['m1'][0]}/t/{page}#m{aid}")
    assert second.findtext("a:id", namespaces=NS) == first.findtext(
        "a:id", namespaces=NS
    )
    canonical = re.search(
        r'<link rel="canonical" href="([^"]+)"',
        client.get(message_url).get_data(as_text=True),
    ).group(1)
    assert link(second) == f"{canonical}#m{aid}"
    assert "ETag" not in response.headers
    assert "Last-Modified" not in response.headers


def seed_dates(dates):
    with SessionLocal() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        ids = []
        for i, date in enumerate(dates):
            article = Article(
                message_id=f"feed-{i}@example",
                author="Feed Author <feed@example>",
                date=date,
            )
            session.add(article)
            session.flush()
            session.add(
                ArticleList(
                    article_id=article.id,
                    inbox_id=inbox.id,
                    epoch="0.git",
                    commit_sha=f"{i:040x}",
                    thread_root_id=article.id,
                )
            )
            ids.append(article.id)
        session.commit()
        return ids


@pytest.mark.parametrize("suffix", ["", "/author/feed@example"])
def test_feed_five_day_boundary_expires_inside_warm_listing(
    client, frozen_clock, suffix
):
    now = datetime.now(UTC)
    floor = now - timedelta(days=5)
    ids = seed_dates(
        [floor - timedelta(seconds=1), floor, floor + timedelta(seconds=1), None]
    )
    first = entries(client.get(f"/alpha{suffix}/feed.atom"))
    assert list(first) == [ids[2], ids[1]]
    assert (
        cache.get(f"feed_articles:alpha:5:1000:{'feed@example' if suffix else ''}")
        is not None
    )
    frozen_clock.tick(timedelta(seconds=1))
    second = entries(client.get(f"/alpha{suffix}/feed.atom"))
    assert list(second) == [ids[2]]


@pytest.mark.parametrize("suffix", ["", "/author/feed@example"])
@pytest.mark.parametrize("one_thread", [False, True])
def test_feed_caps_at_newest_thousand(client, frozen_clock, suffix, one_thread):
    now = datetime.now(UTC)
    ids = seed_dates([now - timedelta(seconds=i) for i in range(1002)])
    if one_thread:
        with SessionLocal() as session:
            root = session.get(Article, ids[-1])
            for member in session.scalars(
                select(ArticleList).where(ArticleList.article_id.in_(ids))
            ):
                member.thread_root_id = root.id
            for article in session.scalars(
                select(Article).where(Article.id.in_(ids[:-1]))
            ):
                article.thread_parent = root.message_id
            session.commit()
        assert root.id not in ids[:1000]
    statements = []
    with SessionLocal() as session:
        engine = session.get_bind()

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.get(f"/alpha{suffix}/feed.atom")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert list(entries(response)) == ids[:1000]
    assert len(statements) < 25, len(statements)
    if one_thread:
        page = (len(ids) - 1) // settings.thread_view_render_cap + 1
        assert link(entries(response)[ids[0]]).endswith(
            f"/{ids[-1]}/t/{page}#m{ids[0]}"
        )


@pytest.mark.parametrize("suffix", ["", "/author/feed@example"])
def test_warm_feed_omits_reindexed_entry_after_link_ttl(client, frozen_clock, suffix):
    ids = seed_dates([datetime.now(UTC), datetime.now(UTC) - timedelta(seconds=1)])
    path = f"/alpha{suffix}/feed.atom"
    with cache.warm_window(7200):
        assert list(entries(client.get(path))) == ids
    key = f"feed_articles:alpha:5:1000:{'feed@example' if suffix else ''}"
    assert cache.get(key) is not None
    with SessionLocal() as session:
        session.execute(delete(ArticleList).where(ArticleList.article_id == ids[0]))
        session.commit()
    assert list(entries(client.get(path))) == ids
    frozen_clock.tick(timedelta(seconds=LISTING_CACHE_TTL_SEC + 1))
    assert cache.get(key) is not None
    response = client.get(path)
    assert list(entries(response)) == [ids[1]]


@pytest.mark.parametrize("author", ["", "feed@example"])
@pytest.mark.parametrize("total", [1, RECENT_INBOX_FIRST_BELOW_ARTICLES, None])
def test_feed_listing_uses_known_small_inbox_plan(
    seeded_db, frozen_clock, total, author
):
    with seeded_db() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
    if total is not None:
        _cache_total(inbox, total)
    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as session:
        plan = plan_of(
            session,
            lambda: feed_articles(session, inbox, author=author),
            "AND a.date >=",
        )
    expected = "ix_article_lists_inbox_id" if total == 1 else "ix_articles_date"
    assert expected in plan, plan


def test_canonical_metadata_seeks_article_memberships(seeded_db):
    with planner_stats(seeded_db, PRODUCTION_STATS), seeded_db() as session:
        articles = list(session.scalars(select(Article)))
        assert len(articles) == 4
        plan = plan_of(
            session,
            lambda: _canonical_destinations_for(session, articles),
            "LEFT OUTER JOIN articles AS canonical_root",
        )
    assert "SEARCH articles USING INTEGER PRIMARY KEY" in plan, plan
    assert "SEARCH article_lists USING INDEX" in plan, plan
    assert "(article_id=?)" in plan, plan
    assert "SEARCH canonical_root USING INTEGER PRIMARY KEY" in plan, plan
