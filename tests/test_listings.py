"""The listing cache preserves full roots before presentation filters."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from mimir.listings import ListingThread, pack_threads, unpack_threads


def test_compact_listing_round_trip_preserves_root_metadata():
    row = ListingThread(
        12,
        "alpha",
        "m@example.org",
        "[PATCH 0/2] café",
        None,
        datetime(2024, 3, 1, tzinfo=UTC),
        4,
        3,
        datetime(2024, 3, 2, tzinfo=UTC),
        0,
        "series-key",
    )
    payload = pack_threads("alpha", [row])
    assert payload["rows"][0][-2:] == [0, "series-key"]
    assert unpack_threads(payload) == [row]


def test_compressed_cache_round_trip_and_wire_size():
    import json

    from mimir.broker.protocol import CacheSetRequest
    from mimir.listings import decode_listing_payload, encode_listing_payload

    payload = {"inbox": "alpha", "rows": [[None, "café 雪", 0, '\\"quoted']] * 1000}
    encoded = encode_listing_payload(payload)
    assert encoded["codec"] == "zlib-json-v1"
    assert decode_listing_payload(encoded) == payload
    value = json.dumps(encoded, separators=(",", ":"))
    wire = (
        CacheSetRequest(
            key="v4:listing_threads_v1:alpha:test", value_json=value, ttl=300, rpc_id=1
        )
        .model_dump_json()
        .encode()
        + b"\n"
    )
    assert len(value.encode()) < 8 * 1024 * 1024
    assert len(wire) < 16 * 1024 * 1024
    assert len(value) < len(json.dumps(payload, separators=(",", ":")))


def test_daily_paging_and_indexing(client, monkeypatch):
    from mimir.web.routes import timeviews

    now = datetime(2024, 3, 1, tzinfo=UTC)
    rows = [
        ListingThread(
            n, "alpha", f"{n}@x", f"thread-{n}", "Sender", now, 1, 0, now, None, None
        )
        for n in range(1000, 1101)
    ]
    monkeypatch.setattr(timeviews, "listing_for_day", lambda *args: rows)
    body = client.get("/alpha/today?page=2").data.decode()
    assert 'name="robots" content="noindex"' in body
    assert 'rel="canonical"' not in body
    assert "thread-1100" in body
    assert "thread-1000" not in body
    assert "Previous 100" in body
    assert client.get("/alpha/today?page=3").status_code == 404


@pytest.mark.parametrize("path", ["today", "yesterday"])
def test_every_daily_page_is_noindex_and_filter_links_preserve_state(
    client, monkeypatch, path
):
    from mimir.web.routes import timeviews

    now = datetime(2024, 3, 1, tzinfo=UTC)
    rows = [
        ListingThread(
            n,
            "alpha",
            f"{n}@x",
            f"[PATCH] thread-{n}",
            "Sender",
            now,
            1,
            0,
            now,
            None,
            None,
        )
        for n in range(1000, 1101)
    ]
    monkeypatch.setattr(timeviews, "listing_for_day", lambda *args: rows)
    for page in (1, 2):
        body = client.get(f"/alpha/{path}?filter=patches&page={page}").get_data(
            as_text=True
        )
        assert 'name="robots" content="noindex"' in body
        assert 'rel="canonical"' not in body
        assert "nofollow" not in body
        assert f"/alpha/{path}?filter=patches" in body
        assert 'aria-current="page">Patches' in body
    first = client.get(f"/alpha/{path}?filter=patches").get_data(as_text=True)
    assert f"/alpha/{path}?filter=patches&amp;page=2" in first
    assert first.count('class="listing-row"') == 100


def test_pull_sql_and_python_forms_agree(seeded_db):
    from sqlalchemy import literal

    from mimir.pull_requests import is_pull_subject, pull_subject_sql

    subjects = [
        None,
        "[GIT PULL] topic",
        "[git pull] topic",
        "Re: [GIT PULL] topic",
        " [GIT PULL] topic",
        "[GİT PULL] topic",
        "[GIT PULL]",
        "[GIT PULLER] topic",
    ]
    with seeded_db() as session:
        for subject in subjects:
            assert bool(
                session.scalar(select(pull_subject_sql(literal(subject))))
            ) == is_pull_subject(subject)


@pytest.mark.parametrize(
    "url",
    [
        "/alpha/today?page=0",
        "/alpha/today?page=-1",
        "/alpha/today?page=x",
        "/alpha/today?filter=bogus",
        "/alpha/2024/03/?page=2",
    ],
)
def test_listing_options_reject_invalid_urls(client, url):
    assert client.get(url).status_code == 404


@pytest.mark.parametrize(
    "subject,position,key,patch,pull",
    [
        ("[PATCH] solo", None, None, True, False),
        ("[PATCH 0/2] cover", 0, "key", True, False),
        ("[PATCH 1/2] orphan reply", None, None, False, False),
        ("Re: [PATCH] reply", None, None, False, False),
        ("[git pull] topic", None, None, False, True),
        ("Re: [GIT PULL] reply", None, None, False, False),
        (None, None, None, False, False),
    ],
)
def test_filters_share_original_patch_and_pull_rules(
    subject, position, key, patch, pull
):
    from mimir.listings import filter_threads

    row = ListingThread(
        1, "alpha", "m@x", subject, None, None, 1, 0, None, position, key
    )
    assert bool(filter_threads([row], "patches")) == patch
    assert bool(filter_threads([row], "pulls")) == pull
    assert filter_threads([row], "unanswered") == [row]


def test_month_filters_before_cap_and_loads_only_displayed_lifecycle(
    client, seeded_db, monkeypatch
):
    from mimir.models import Article, ArticleList, Inbox
    from mimir.web.routes import timeviews

    now = datetime(2024, 3, 10, tzinfo=UTC)
    with seeded_db() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        for i in range(3):
            article = Article(
                message_id=f"listing-{i}@x",
                subject="[PATCH] wanted" if i == 0 else f"prose-{i}",
                author="Name <private@example.net>",
                date=now.replace(day=10 + i),
            )
            session.add(article)
            session.flush()
            session.add(
                ArticleList(
                    article_id=article.id,
                    inbox_id=inbox.id,
                    epoch="0.git",
                    commit_sha="a" * 40,
                )
            )
        session.commit()
    seen = []
    monkeypatch.setattr(timeviews, "MONTH_THREAD_CAP", 1)
    monkeypatch.setattr(
        timeviews,
        "lifecycle_status_for_articles",
        lambda s, ids, inbox: seen.append(ids) or {},
    )
    response = client.get("/alpha/2024/03/?filter=patches")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "[PATCH] wanted" in body
    assert "prose-2" not in body
    assert "private@example.net" not in body
    assert len(seen) == 1 and len(seen[0]) == 1
    assert 'name="robots" content="noindex"' in body
    assert 'rel="canonical"' not in body


def test_listing_cache_round_trip_and_old_cache_isolation(seeded_db, monkeypatch):
    from mimir import cache, listings
    from mimir.models import Inbox

    inbox = Inbox(id=1, name="alpha", mirror_path="/unused")
    now = datetime(2024, 3, 1, tzinfo=UTC)
    row = ListingThread(
        12, "alpha", "m@x", "[PATCH] café", None, now, 1, 0, now, None, None
    )
    calls = []
    monkeypatch.setattr(
        listings, "listing_query", lambda *args: calls.append(1) or [row]
    )
    cache.set("threads_for_day:alpha:2024-03-01", [], 300)
    with seeded_db() as session:
        assert listings.listing_for_day(session, inbox, now.date()) == [row]
        assert listings.listing_for_day(session, inbox, now.date()) == [row]
    assert calls == [1]
    assert cache.get("threads_for_day:alpha:2024-03-01") == []


def test_listing_overflow_returns_complete_results_without_caching(
    seeded_db, monkeypatch, caplog
):
    from mimir import cache, listings
    from mimir.models import Inbox

    now = datetime(2024, 3, 1, tzinfo=UTC)
    inbox = Inbox(id=1, name="alpha", mirror_path="/unused")
    row = ListingThread(
        12, "alpha", "m@x", _incompressible_subject(), None, now, 1, 0, now, None, None
    )
    monkeypatch.setattr(cache, "MAX_CACHE_VALUE_BYTES", 300)
    monkeypatch.setattr(listings, "listing_query", lambda *args: [row])
    with seeded_db() as session:
        assert listings.listing_for_day(session, inbox, now.date()) == [row]
    assert cache.get("listing_threads_v1:alpha:day:2024-03-01") is None
    assert "exceeds" in caplog.text


def test_listing_query_plan_uses_scoped_membership(seeded_db):
    from mimir.listings import listing_query
    from mimir.models import Inbox
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    with planner_stats(seeded_db, PRODUCTION_STATS):
        with seeded_db() as session:
            inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
            plan = plan_of(
                session,
                lambda: listing_query(
                    session,
                    inbox,
                    datetime(2024, 1, 1, tzinfo=UTC),
                    datetime(2024, 4, 1, tzinfo=UTC),
                ),
                "WITH RECURSIVE",
                required_sql=(
                    "article_lists al INDEXED BY sqlite_autoindex_article_lists_1",
                ),
            )
    assert "SEARCH al USING INDEX ix_article_lists_inbox_id (inbox_id=?)" in plan
    assert "SEARCH a USING INDEX ix_articles_message_id (message_id=?)" in plan
    assert (
        "SEARCH al USING COVERING INDEX sqlite_autoindex_article_lists_1 (article_id=? AND inbox_id=?)"
        in plan
    )
    assert "SCAN a" not in plan
    assert "SCAN al" not in plan


@pytest.mark.parametrize(
    "label",
    [
        "threads_for_day (today)",
        "threads_for_day (yesterday)",
        "listing current month",
        "listing since maximum",
    ],
)
def test_oversized_listing_is_reported_as_failed_warm(seeded_db, monkeypatch, label):
    from datetime import date, timedelta

    from mimir import cache, listings
    from mimir.broker.handlers.warm import _run_targets
    from mimir.cli.cache import _build_slow_inbox_targets
    from mimir.models import Inbox

    now = datetime(2024, 1, 15, tzinfo=UTC)
    row = ListingThread(
        12, "alpha", "m@x", _incompressible_subject(), None, now, 1, 0, now, None, None
    )
    monkeypatch.setattr(listings, "listing_query", lambda *args: [row])
    with seeded_db() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
    today = date(2024, 1, 15)
    targets = [
        t
        for t in _build_slow_inbox_targets(inbox, today, today - timedelta(days=1))
        if t[0] == f"alpha {label}"
    ]
    assert len(targets) == 1
    monkeypatch.setattr(cache, "MAX_CACHE_VALUE_BYTES", 300)
    warmed, errors, _, _ = _run_targets(targets)
    assert not warmed
    assert len(errors) == 1
    assert "Listing exceeds cache value limit" in errors[0]


def test_slow_warm_listing_expiry_and_route_key_match(seeded_db, monkeypatch):
    from datetime import date, timedelta

    from freezegun import freeze_time

    from mimir import cache, listings
    from mimir.broker.handlers.warm import _run_targets
    from mimir.cli.cache import _build_slow_inbox_targets
    from mimir.config import settings
    from mimir.models import CacheEntry, Inbox

    calls = []
    monkeypatch.setattr(listings, "listing_query", lambda *args: calls.append(1) or [])
    today = date(2024, 1, 15)
    with seeded_db() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
    targets = [
        t
        for t in _build_slow_inbox_targets(inbox, today, today - timedelta(days=1))
        if t[0] == "alpha threads_for_day (today)"
    ]
    assert len(targets) == 1
    with freeze_time("2024-01-15 12:00:00"):
        assert _run_targets(targets)[0] == [targets[0][0]]
        assert _run_targets(targets)[0] == [targets[0][0]]
        with seeded_db() as session:
            assert listings.listing_for_day(session, inbox, today) == []
            entry = session.get(
                CacheEntry, cache._ns("listing_threads_v1:alpha:day:2024-01-15")
            )
            assert (
                entry.expires_at
                == cache._now() + 300 + settings.warm_cache_slow_refresh_window_sec
            )
    assert calls == [1]


def _incompressible_subject():
    import random

    return random.Random(991).randbytes(4096).hex()


def test_compressible_listing_caches_and_warms_despite_raw_size(seeded_db, monkeypatch):
    import json

    from mimir import cache, listings
    from mimir.models import Inbox

    now = datetime(2024, 3, 1, tzinfo=UTC)
    inbox = Inbox(id=1, name="alpha", mirror_path="/unused")
    row = ListingThread(
        12, "alpha", "m@x", "repeated" * 10000, None, now, 1, 0, now, None, None
    )
    raw = pack_threads("alpha", [row])
    assert len(json.dumps(raw)) > 1000
    assert len(json.dumps(listings.encode_listing_payload(raw))) < 1000
    monkeypatch.setattr(cache, "MAX_CACHE_VALUE_BYTES", 1000)
    monkeypatch.setattr(listings, "listing_query", lambda *args: [row])
    with seeded_db() as session:
        assert listings.listing_for_day(session, inbox, now.date()) == [row]
    assert (
        cache.get("listing_threads_v1:alpha:day:2024-03-01")["codec"] == "zlib-json-v1"
    )
    listings.warm_listing([row])


def test_compressed_listing_rejects_unknown_codec():
    from mimir.listings import decode_listing_payload

    with pytest.raises(ValueError, match="Unknown listing cache codec"):
        decode_listing_payload({"codec": "unknown", "data": ""})


@pytest.mark.parametrize(
    "change", ["short", "long", "boolean", "truncated", "trailing"]
)
def test_compressed_listing_rejects_corrupt_size_or_stream(change):
    import base64

    from mimir.listings import decode_listing_payload, encode_listing_payload

    payload = encode_listing_payload({"inbox": "alpha", "rows": [["x" * 10000]]})
    if change == "short":
        payload["raw_bytes"] = 2
    elif change == "long":
        payload["raw_bytes"] += 1
    elif change == "boolean":
        payload["raw_bytes"] = True
    else:
        compressed = base64.b64decode(payload["data"])
        payload["data"] = base64.b64encode(
            compressed[:-1] if change == "truncated" else compressed + b"extra"
        ).decode()
    with pytest.raises(ValueError, match="Invalid listing"):
        decode_listing_payload(payload)
