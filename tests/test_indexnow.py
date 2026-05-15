"""IndexNow push-notification client.

These tests pin the wire shape (host, key, keyLocation, urlList),
the off-by-default behaviour (no key → no call, no base URL → no
call), and the message-IDs-to-URLs bridge that the `update` CLI
uses. The HTTP transport is monkeypatched at `urllib.request.urlopen`
so no real network traffic flies during tests.
"""
import json

import pytest
from sqlalchemy import select

from mimir import indexnow
from mimir.config import settings
from mimir.models import Article


class _StubResponse:
    """Context-manager-compatible urlopen() return-value stand-in."""
    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return b""


@pytest.fixture
def captured_indexnow(monkeypatch):
    """Replaces `urllib.request.urlopen` (as imported into
    `mimir.indexnow`) with a recorder. Returns the captured-calls
    list so tests can introspect host, payload, headers."""
    calls: list[dict] = []

    def _fake_urlopen(req, timeout=None):
        body = req.data
        payload = json.loads(body.decode("utf-8")) if body else None
        calls.append({
            "url": req.full_url,
            "method": req.get_method(),
            "headers": dict(req.headers),
            "payload": payload,
            "timeout": timeout,
        })
        return _StubResponse(status=200)

    monkeypatch.setattr(indexnow, "urlopen", _fake_urlopen)
    return calls


def test_notify_noop_when_key_unset(captured_indexnow, monkeypatch):
    """The feature is opt-in: no key set → notify returns 0 and does
    not hit the network. Lets the scheduler call notify
    unconditionally without a config guard at every call site."""
    monkeypatch.setattr(settings, "indexnow_key", None)
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")
    submitted = indexnow.notify(["https://example.test/lkml/2024/01/1"])
    assert submitted == 0
    assert captured_indexnow == []


def test_notify_noop_when_base_url_unset(captured_indexnow, monkeypatch):
    """Key set but no `site_base_url` → can't build keyLocation, so
    skip the call and log a warning. Unreachable in production where
    site_base_url is wired, but the dev path should fail safe."""
    monkeypatch.setattr(settings, "indexnow_key", "abc123" * 5)
    monkeypatch.setattr(settings, "site_base_url", "")
    submitted = indexnow.notify(["https://example.test/lkml/2024/01/1"])
    assert submitted == 0
    assert captured_indexnow == []


def test_notify_noop_on_empty_url_list(captured_indexnow, monkeypatch):
    """Steady-state ticks with no new articles → empty url list →
    notify returns 0 without a call. Avoids spamming IndexNow with
    empty-payload requests."""
    monkeypatch.setattr(settings, "indexnow_key", "abc123" * 5)
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")
    submitted = indexnow.notify([])
    assert submitted == 0
    assert captured_indexnow == []


def test_notify_posts_expected_payload(captured_indexnow, monkeypatch):
    """The wire shape, exactly what IndexNow's spec requires.
    Asserts host (from site_base_url), key, keyLocation, urlList,
    content-type, and HTTP method. Drift here would break Bing
    discovery for everyone who turns the feature on."""
    monkeypatch.setattr(settings, "indexnow_key", "deadbeef" * 4)
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")
    monkeypatch.setattr(
        settings, "indexnow_endpoint", "https://api.indexnow.org/indexnow",
    )
    urls = [
        "https://example.test/lkml/2024/01/1",
        "https://example.test/lkml/2024/01/2",
    ]
    submitted = indexnow.notify(urls)
    assert submitted == 2
    assert len(captured_indexnow) == 1
    call = captured_indexnow[0]
    assert call["url"] == "https://api.indexnow.org/indexnow"
    assert call["method"] == "POST"
    # urllib normalises header names to title-case; check both
    # capitalisations.
    ct = call["headers"].get("Content-type") or call["headers"].get("Content-Type")
    assert "application/json" in ct
    payload = call["payload"]
    assert payload["host"] == "example.test"
    assert payload["key"] == "deadbeef" * 4
    assert payload["keyLocation"] == (
        "https://example.test/" + "deadbeef" * 4 + ".txt"
    )
    assert payload["urlList"] == urls


def test_notify_chunks_above_protocol_limit(captured_indexnow, monkeypatch):
    """Protocol caps a single POST at 10k URLs. Spawning more than
    that should split into multiple requests, each under the cap.
    Defensive: `indexnow_max_per_tick` defaults to 1000 (well under
    10k), but the chunking is the correct shape if an operator
    raises the cap."""
    monkeypatch.setattr(settings, "indexnow_key", "k" * 32)
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")
    monkeypatch.setattr(indexnow, "INDEXNOW_MAX_URLS_PER_REQUEST", 3)
    urls = [f"https://example.test/lkml/2024/01/{i}" for i in range(7)]
    submitted = indexnow.notify(urls)
    assert submitted == 7
    assert [len(c["payload"]["urlList"]) for c in captured_indexnow] == [3, 3, 1]


def test_notify_swallows_network_errors(monkeypatch, caplog):
    """Best-effort: a network exception must not surface to the
    caller. The scheduler tick keeps going; the sitemap is the
    durable signal."""
    from urllib.error import URLError
    monkeypatch.setattr(settings, "indexnow_key", "k" * 32)
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")

    def _raises(req, timeout=None):
        raise URLError("connection refused")

    monkeypatch.setattr(indexnow, "urlopen", _raises)
    with caplog.at_level("WARNING", logger="mimir.indexnow"):
        submitted = indexnow.notify(["https://example.test/x"])
    assert submitted == 0
    assert any("network error" in r.message for r in caplog.records)


def test_build_urls_uses_canonical_inbox(seeded_db):
    """A cross-posted article's IndexNow URL must point at its
    canonical inbox, not at every inbox it's linked to. Otherwise
    we'd push duplicate URLs for the same content, diluting the
    discovery signal."""
    # Seeded fixture has `art2@example.com` in beta. Cross-post it
    # to alpha so two ArticleList rows exist, then pin canonical to
    # alpha.
    from mimir.models import ArticleList, Inbox
    with seeded_db() as s:
        article = s.execute(
            select(Article).where(Article.message_id == "art2@example.com")
        ).scalar_one()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.add(ArticleList(
            article_id=article.id, inbox_id=alpha.id,
            epoch="0.git", commit_sha="x" * 40,
        ))
        article.canonical_inbox_id = alpha.id
        s.commit()
        article_id = article.id
        article_date = article.date

    with seeded_db() as s:
        urls = indexnow.build_urls(
            s, ["art2@example.com"], base="https://example.test",
        )
    assert len(urls) == 1
    assert urls[0] == (
        f"https://example.test/alpha/{article_date.year}/"
        f"{article_date.month:02d}/{article_id}"
    )


def test_build_urls_skips_unknown_message_ids(seeded_db):
    """Message IDs that don't resolve to an Article (race between
    ingest commit and update's post-pass, unlikely but possible
    if a future change deferred article visibility) just drop out
    silently. Better than emitting URLs that 404."""
    with seeded_db() as s:
        urls = indexnow.build_urls(
            s, ["does-not-exist@example.com"], base="https://example.test",
        )
    assert urls == []


def test_indexnow_key_route_404s_when_unset(client, monkeypatch):
    """An unconfigured deploy must not expose the verification
    endpoint at all. Confirms `create_app` doesn't register the
    route when `indexnow_key` is None. Follows redirects because
    Flask's `/<inbox_name>/` rule 308s missing-slash variants
    before resolving the inbox; the destination 404s, which is
    what we actually care about."""
    monkeypatch.setattr(settings, "indexnow_key", None)
    r = client.get("/somekey.txt", follow_redirects=True)
    assert r.status_code == 404


def test_indexnow_key_route_serves_key_when_set(monkeypatch):
    """When `indexnow_key` is set at create_app time, /<key>.txt
    serves the key as text/plain. Tied to the spec's ownership-
    verification step: IndexNow fetches this URL to confirm the
    submitter controls the host."""
    monkeypatch.setattr(settings, "indexnow_key", "abc1234567890abcdef")
    from mimir import create_app
    c = create_app().test_client()
    r = c.get("/abc1234567890abcdef.txt")
    assert r.status_code == 200
    assert r.data == b"abc1234567890abcdef"
    assert r.mimetype == "text/plain"


def test_indexnow_key_route_only_matches_configured_key(monkeypatch):
    """The route is registered at the literal key path, so any
    other path returns 404, even one that ends in `.txt`. Pins
    that we're not exposing a `/<arbitrary>.txt` catchall."""
    monkeypatch.setattr(settings, "indexnow_key", "real-key-value-here")
    from mimir import create_app
    c = create_app().test_client()
    assert c.get("/real-key-value-here.txt").status_code == 200
    # Non-matching `.txt` paths still 404 (after following the
    # `/<inbox_name>/` add-slash redirect).
    assert c.get("/some-other-string.txt", follow_redirects=True).status_code == 404


def test_update_skips_push_when_above_per_tick_cap(monkeypatch, caplog):
    """Backfill guard: when `update` produces more new articles
    than `indexnow_max_per_tick`, skip the push entirely (do not
    truncate to the cap; that's still a backfill, just slower).
    The sitemap is the durable discovery path for the backlog."""
    from mimir.cli import _push_indexnow
    notify_calls = []
    monkeypatch.setattr(
        indexnow, "notify", lambda urls: notify_calls.append(urls) or 0,
    )
    monkeypatch.setattr(settings, "indexnow_key", "k" * 32)
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")
    monkeypatch.setattr(settings, "indexnow_max_per_tick", 5)

    # Six message IDs > cap of 5 → skip.
    with caplog.at_level("WARNING", logger="mimir.cli"):
        _push_indexnow([f"m{i}@example.com" for i in range(6)])
    assert notify_calls == []
    assert any(
        "exceeds INDEXNOW_MAX_PER_TICK" in r.message for r in caplog.records
    )


def test_update_no_op_when_no_new_messages(monkeypatch):
    """Steady-state tick with no new articles: don't open a session
    or call notify."""
    from mimir.cli import _push_indexnow
    notify_calls = []
    monkeypatch.setattr(
        indexnow, "notify", lambda urls: notify_calls.append(urls) or 0,
    )
    monkeypatch.setattr(settings, "indexnow_key", "k" * 32)
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")

    _push_indexnow([])
    assert notify_calls == []


def test_update_no_op_when_key_unset(monkeypatch):
    """Even with new articles, an unconfigured operator never sees
    a push. notify itself also guards this, but the CLI short-
    circuits to avoid a wasted DB query in the dev path."""
    from mimir.cli import _push_indexnow
    notify_calls = []
    monkeypatch.setattr(
        indexnow, "notify", lambda urls: notify_calls.append(urls) or 0,
    )
    monkeypatch.setattr(settings, "indexnow_key", None)

    _push_indexnow(["m@example.com"])
    assert notify_calls == []


def test_update_echoes_one_line_on_successful_push(
    seeded_db, monkeypatch, capsys,
):
    """Successful submissions surface in default scheduler output
    via click.echo, not just the INFO-level log (which is hidden at
    default verbosity). Mirrors the per-epoch `name/epoch: new=N
    ...` lines, anything in the scheduler journal signals a real
    event."""
    from mimir.cli import _push_indexnow
    monkeypatch.setattr(indexnow, "notify", lambda urls: len(urls))
    monkeypatch.setattr(settings, "indexnow_key", "k" * 32)
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")
    monkeypatch.setattr(settings, "indexnow_max_per_tick", 1000)

    # `build_urls` runs against the test DB. The seeded fixture has
    # `art2@example.com` in beta, use it so build_urls returns a
    # non-empty list and notify reports submitted > 0.
    _push_indexnow(["art2@example.com"])
    captured = capsys.readouterr()
    assert "indexnow: pushed 1 URL(s)" in captured.out


def test_update_no_echo_when_notify_returns_zero(
    seeded_db, monkeypatch, capsys,
):
    """notify swallows failures and returns 0 on network/HTTP
    errors. In that case there's no successful push to announce , 
    the warning log inside notify already covers the failure; we
    don't want a second misleading "pushed 0 URL(s)" line."""
    from mimir.cli import _push_indexnow
    monkeypatch.setattr(indexnow, "notify", lambda urls: 0)
    monkeypatch.setattr(settings, "indexnow_key", "k" * 32)
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")
    monkeypatch.setattr(settings, "indexnow_max_per_tick", 1000)

    _push_indexnow(["art2@example.com"])
    captured = capsys.readouterr()
    assert "indexnow:" not in captured.out
