"""IndexNow push-notification client.

These tests pin the wire shape (host, key, keyLocation, urlList),
the off-by-default behaviour (no key → no call, no base URL → no
call), and the message-IDs-to-URLs bridge that the `update` CLI
uses. The HTTP transport is monkeypatched at
`mimir._outbound.OUTBOUND_OPENER.open` (the hardened opener used by
the production caller) so no real network traffic flies during tests.
"""

import re

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
    """Replaces the `.open` method on `mimir.indexnow.OUTBOUND_OPENER`
    (the hardened opener used by the production caller) with a
    recorder. Returns the captured-calls list so tests can
    introspect host, payload, headers."""
    calls: list[dict] = []

    def _fake_open(req, timeout=None):
        body = req.data
        payload = json.loads(body.decode("utf-8")) if body else None
        calls.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "headers": dict(req.headers),
                "payload": payload,
                "timeout": timeout,
            }
        )
        return _StubResponse(status=200)

    monkeypatch.setattr(indexnow.OUTBOUND_OPENER, "open", _fake_open)
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
        settings,
        "indexnow_endpoint",
        "https://api.indexnow.org/indexnow",
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
    assert payload["keyLocation"] == ("https://example.test/" + "deadbeef" * 4 + ".txt")
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

    monkeypatch.setattr(indexnow.OUTBOUND_OPENER, "open", _raises)
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
        s.add(
            ArticleList(
                article_id=article.id,
                inbox_id=alpha.id,
                epoch="0.git",
                commit_sha="x" * 40,
            )
        )
        article.canonical_inbox_id = alpha.id
        s.commit()
        article_id = article.id
        article_date = article.date

    with seeded_db() as s:
        urls = indexnow.build_urls(
            s,
            ["art2@example.com"],
            base="https://example.test",
        )
    assert len(urls) == 1
    assert urls[0] == (
        f"https://example.test/alpha/{article_date.year}/"
        f"{article_date.month:02d}/{article_id}"
    )


def test_build_urls_falls_back_when_canonical_inbox_vanished(seeded_db):
    """`canonical_inbox_id` FK is `ON DELETE SET NULL`, so deleting
    an inbox between ingest and the next update tick leaves
    cross-posted articles pointing at NULL. `build_urls` must still
    emit a URL (using the alphabetical-first fallback that the web
    `<link rel="canonical">` also uses) instead of dropping the
    push. Pins the canonical-pick rule's behaviour at the FK
    SET-NULL race boundary."""
    from mimir.models import ArticleList, Inbox

    with seeded_db() as s:
        article = s.execute(
            select(Article).where(Article.message_id == "art2@example.com")
        ).scalar_one()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.add(
            ArticleList(
                article_id=article.id,
                inbox_id=alpha.id,
                epoch="0.git",
                commit_sha="x" * 40,
            )
        )
        # Simulate the FK SET NULL state: cross-posted but no
        # canonical pin.
        article.canonical_inbox_id = None
        s.commit()
        article_id = article.id
        article_date = article.date

    with seeded_db() as s:
        urls = indexnow.build_urls(
            s,
            ["art2@example.com"],
            base="https://example.test",
        )
    # Falls back to alphabetical first: alpha before beta.
    assert urls == [
        f"https://example.test/alpha/{article_date.year}/"
        f"{article_date.month:02d}/{article_id}"
    ]


def test_build_urls_skips_unknown_message_ids(seeded_db):
    """Message IDs that don't resolve to an Article (race between
    ingest commit and update's post-pass, unlikely but possible
    if a future change deferred article visibility) just drop out
    silently. Better than emitting URLs that 404."""
    with seeded_db() as s:
        urls = indexnow.build_urls(
            s,
            ["does-not-exist@example.com"],
            base="https://example.test",
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
        indexnow,
        "notify",
        lambda urls: notify_calls.append(urls) or 0,
    )
    monkeypatch.setattr(settings, "indexnow_key", "k" * 32)
    monkeypatch.setattr(settings, "site_base_url", "https://example.test")
    monkeypatch.setattr(settings, "indexnow_max_per_tick", 5)

    # Six message IDs > cap of 5 → skip.
    with caplog.at_level("WARNING", logger="mimir.cli"):
        _push_indexnow([f"m{i}@example.com" for i in range(6)])
    assert notify_calls == []
    assert any("exceeds INDEXNOW_MAX_PER_TICK" in r.message for r in caplog.records)


def test_update_no_op_when_no_new_messages(monkeypatch):
    """Steady-state tick with no new articles: don't open a session
    or call notify."""
    from mimir.cli import _push_indexnow

    notify_calls = []
    monkeypatch.setattr(
        indexnow,
        "notify",
        lambda urls: notify_calls.append(urls) or 0,
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
        indexnow,
        "notify",
        lambda urls: notify_calls.append(urls) or 0,
    )
    monkeypatch.setattr(settings, "indexnow_key", None)

    _push_indexnow(["m@example.com"])
    assert notify_calls == []


def test_update_echoes_one_line_on_successful_push(
    seeded_db,
    monkeypatch,
    capsys,
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
    seeded_db,
    monkeypatch,
    capsys,
):
    """notify swallows failures and returns 0 on network/HTTP
    errors. In that case there's no successful push to announce
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


def test_build_urls_emits_the_thread_url_for_a_multi_message_thread(client, tmp_path):
    """The document that changed is the THREAD, so that is what gets
    pushed.

    Before this, a reply pushed its own message URL, which the page
    itself disclaims via `<link rel="canonical">`, while the
    consolidated page that actually grew was never announced at all.
    That left the one page this workstream wants indexed with no
    signal on the fastest channel mimir has.

    Seeded through real ingest rather than direct INSERT, because
    `thread_root_id` is maintained BY ingest and a hand-built row would
    leave it NULL and silently exercise the fallback instead.
    """
    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [("in1@x", None), ("in2@x", "in1@x"), ("in3@x", "in2@x")],
    )
    root_id, _url = seeded["in1@x"]

    with SessionLocal() as s:
        urls = indexnow.build_urls(
            s, ["in1@x", "in2@x", "in3@x"], base="https://example.test"
        )

    # All three collapse onto one thread URL: deduping is a straight
    # win against the endpoint's per-request URL budget.
    assert len(urls) == 1, urls
    assert urls[0].endswith(f"/{root_id}/t"), urls
    assert urls[0].startswith("https://example.test/alpha/")


def test_build_urls_keeps_the_message_url_for_a_single_message_thread(client, tmp_path):
    """A single-message thread has nothing to consolidate, and its own
    page is the richer one, so it stays its own URL."""
    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("solo@x", None)])
    art_id, _url = seeded["solo@x"]

    with SessionLocal() as s:
        urls = indexnow.build_urls(s, ["solo@x"], base="https://example.test")

    assert len(urls) == 1
    assert urls[0].endswith(f"/{art_id}")
    assert not urls[0].endswith("/t")


def test_build_urls_falls_back_to_the_message_url_before_the_backfill(client, tmp_path):
    """A NULL `thread_root_id` must degrade to the message URL.

    That is the state between the migration and the backfill. Pushing
    nothing would lose the notification; guessing a thread URL without
    a known root would push a URL that may not exist.
    """
    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("nb1@x", None), ("nb2@x", "nb1@x")])
    with SessionLocal() as s:
        s.execute(update(ArticleList).values(thread_root_id=None))
        s.commit()

    with SessionLocal() as s:
        urls = indexnow.build_urls(s, ["nb1@x", "nb2@x"], base="https://example.test")

    assert len(urls) == 2, urls
    assert all(not u.endswith("/t") for u in urls), urls
    assert {u.rsplit("/", 1)[-1] for u in urls} == {
        str(seeded["nb1@x"][0]),
        str(seeded["nb2@x"][0]),
    }


def test_build_urls_only_pushes_urls_that_resolve_and_self_canonicalise(
    client, tmp_path
):
    """Fetch everything we push and check the destination agrees.

    One assertion that would have caught both blocking bugs review
    found: a URL built for an inbox the article is not linked to (404),
    and a singleton thread handed a sibling inbox's thread URL (200 but
    self-canonicalising to a DIFFERENT url, i.e. we advertise a
    duplicate-content page nothing else lists).

    `build_urls`'s own contract is that a dangling URL is worse than a
    missed notification, and nothing was checking it.
    """
    import re

    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    seed_thread_shape(tmp_path, "alpha", [("rv1@x", None), ("rv2@x", "rv1@x")])
    solo_mirror = tmp_path / "solo"
    solo_mirror.mkdir()
    seed_thread_shape(solo_mirror, "beta", [("rv3@x", None)])

    with SessionLocal() as s:
        urls = indexnow.build_urls(s, ["rv1@x", "rv2@x", "rv3@x"], base="")
    assert urls

    for url in urls:
        resp = client.get(url)
        assert resp.status_code == 200, (
            f"pushed {url} which returned {resp.status_code}"
        )
        html = resp.get_data(as_text=True)
        m = re.search(r'<link rel="canonical" href="([^"]+)"', html)
        assert m, f"pushed {url}, which declares no canonical"
        assert m.group(1).endswith(url), (
            f"pushed {url} but that page canonicalises to {m.group(1)}; "
            "we are advertising a page that disclaims itself"
        )


def test_build_urls_respects_the_inbox_a_thread_is_multi_message_in(client, tmp_path):
    """The same root can head a real thread in one inbox and a
    single-message one in another, because threading is inbox-scoped.

    Deciding on the root alone let a singleton inherit a sibling
    inbox's thread URL. Every earlier test here used ONE inbox, so
    dropping the inbox scoping entirely passed the whole suite.
    """
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("ix1@x", None), ("ix2@x", "ix1@x")])
    root_id = seeded["ix1@x"][0]

    # Cross-post ONLY the root into beta: multi-message in alpha,
    # single-message in beta.
    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        src = (
            s.execute(select(ArticleList).where(ArticleList.article_id == root_id))
            .scalars()
            .first()
        )
        s.add(
            ArticleList(
                article_id=root_id,
                inbox_id=beta.id,
                epoch=src.epoch,
                commit_sha=src.commit_sha,
                thread_root_id=root_id,
            )
        )
        s.execute(
            select(Article).where(Article.id == root_id)
        ).scalar_one().canonical_inbox_id = beta.id
        s.commit()

    # BOTH in one batch, which is what an ingest tick produces and
    # what makes the bug reachable: the reply's pair contributes the
    # "this root has replies" fact, and the root's pair must not
    # inherit it across the inbox boundary.
    with SessionLocal() as s:
        urls = indexnow.build_urls(s, ["ix1@x", "ix2@x"], base="")

    reply_id = seeded["ix2@x"][0]
    assert sorted(urls) == sorted(
        [f"/beta/2024/01/{root_id}", f"/alpha/2024/01/{root_id}/t"]
    ), (
        f"expected beta's singleton copy to stay a message URL and alpha's "
        f"reply to consolidate; got {urls} (reply id {reply_id})"
    )


def test_build_urls_skips_a_canonical_inbox_the_article_is_not_linked_to(
    client, tmp_path
):
    """`canonical_inbox_id` can name an inbox with no `article_lists`
    row: it is assigned at ingest from To:/Cc: list addresses, and
    nothing requires the article to be archived there.

    Resolving to it produces a URL that 404s, which is exactly what
    `build_urls` promises not to emit.
    """
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, Inbox
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("un1@x", None)])
    art_id = seeded["un1@x"][0]

    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        # Addressed to beta's list, archived only in alpha.
        s.execute(
            select(Article).where(Article.id == art_id)
        ).scalar_one().canonical_inbox_id = beta.id
        s.commit()

    with SessionLocal() as s:
        urls = indexnow.build_urls(s, ["un1@x"], base="")

    assert urls == [f"/alpha/2024/01/{art_id}"], urls
    assert client.get(urls[0]).status_code == 200


def test_advertised_url_names_the_page_holding_the_message(
    client, tmp_path, monkeypatch
):
    """IndexNow must announce the page that changed, not page 1.

    A new reply lands on the LAST page, so announcing page 1 named a URL
    that had not changed while the page that grew went unannounced. Both
    the page suffix and the hold-back for threads the column cannot rank
    survived deletion: this is one of the change's headline behaviours
    and had no test at all.
    """
    from mimir.config import settings
    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from mimir.web.urls import _advertised_urls_for

    from tests.test_routes._helpers import build_thread

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=5)
    _root_id, root_url = seeded["m0"]

    with SessionLocal() as s:
        articles = [s.get(Article, aid) for aid, _ in seeded.values()]
        urls = _advertised_urls_for(s, articles, base="")

    # Page membership, read from the rendered pages rather than
    # recomputed, so a shared mistake cannot satisfy both sides.
    page_of = {}
    url = root_url + "/t"
    while url is not None:
        html = client.get(url).get_data(as_text=True)
        for m in re.findall(r'class="thread-message" id="m(\d+)"', html):
            page_of[int(m)] = url
        nxt = re.search(r'<div class="thread-more">\s*<a href="([^"]+)"', html)
        url = nxt.group(1) if nxt else None

    assert len(set(page_of.values())) == 3, "fixture did not paginate"
    for art_id, expected in page_of.items():
        assert urls[art_id] == expected, (
            f"article {art_id} renders on {expected} but IndexNow "
            f"announces {urls[art_id]}"
        )


def test_unrankable_thread_announces_message_urls(client, tmp_path, monkeypatch):
    """A thread the column cannot rank makes no page claims anywhere.

    Including on the push channel: announcing a page number derived from
    a population the route did not use is the same false claim as
    putting it in a canonical.
    """
    from mimir.config import settings
    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from mimir.web.urls import _advertised_urls_for

    from tests.test_routes._helpers import build_thread

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=5, unroot=(2,))

    with SessionLocal() as s:
        articles = [s.get(Article, aid) for aid, _ in seeded.values()]
        urls = _advertised_urls_for(s, articles, base="")

    for art_id, msg_url in ((a, u) for a, u in seeded.values()):
        assert urls[art_id] == msg_url, (
            f"article {art_id} should be announced as its own URL while "
            f"the thread is unrankable; got {urls[art_id]}"
        )


def test_one_inbox_being_unrepaired_does_not_mute_another(
    client, tmp_path, monkeypatch
):
    """Unrankable roots are keyed on the `(inbox, root)` PAIR.

    Root ids are global `articles.id`, so collecting them into a flat set
    made a root flagged in one inbox match in every other: a single
    unrepaired row anywhere dropped page-aware advertising for a HEALTHY
    cross-posted thread elsewhere, and IndexNow then announced page 1 for
    messages whose own pages disclaim it.

    Needs two inboxes that DISAGREE about the same thread, which is why
    a single-inbox fixture could not see it.
    """
    from sqlalchemy import select, update

    from mimir.config import settings
    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from mimir.web.urls import _advertised_urls_for

    from tests.test_routes._helpers import build_thread

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = build_thread(
        tmp_path, "alpha", shape="chain", size=5, cross_post_to="beta"
    )
    ids = [aid for aid, _ in seeded.values()]

    # Alternate the CANONICAL inbox across the thread. `_advertised_urls_for`
    # groups roots by each article's canonical inbox, so with every article
    # canonicalising to one inbox the other is never queried and the flat
    # set is indistinguishable from the keyed one. This is the shape the
    # bug needs: two inboxes both consulted for the same root.
    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        for i, art_id in enumerate(ids):
            s.execute(
                update(Article)
                .where(Article.id == art_id)
                .values(canonical_inbox_id=alpha.id if i % 2 == 0 else beta.id)
            )
        s.execute(
            update(ArticleList)
            .where(
                ArticleList.inbox_id == beta.id,
                ArticleList.article_id == ids[2],
            )
            .values(thread_root_id=None)
        )
        s.commit()
        articles = [s.get(Article, aid) for aid in ids]
        urls = _advertised_urls_for(s, articles, base="")

    paged = [u for u in urls.values() if "/t" in u]
    assert paged, (
        "beta being unrepaired muted alpha's page-aware advertising; "
        f"got {sorted(set(urls.values()))}"
    )
    for art_id, url in urls.items():
        if "/t" not in url:
            continue
        assert client.get(url).status_code == 200
        html = client.get(url).get_data(as_text=True)
        assert f'id="m{art_id}"' in html, (
            f"announced {url} for article {art_id}, which it does not render"
        )
