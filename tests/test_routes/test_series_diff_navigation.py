"""Position navigation shares the diff route's metadata resolution."""

import html
import re

from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.models import Article
from tests.test_routes._helpers import _ingest_series_pair


def series_pair(tmp_path, size=3):
    def posting(version):
        cover = f"v{version}-cover@nav"
        prefix = "PATCH" if version == 1 else f"PATCH v{version}"
        return [
            (
                cover,
                f"[{prefix} 0/{size}] navigation series",
                None,
                f"cover {version}\n".encode(),
                "Alice <a@example>",
            ),
            *[
                (
                    f"v{version}-{pos}@nav",
                    f"[{prefix} {pos}/{size}] change {pos}",
                    cover,
                    f"body {version} position {pos}\n".encode(),
                    "Alice <a@example>",
                )
                for pos in range(1, size + 1)
            ],
        ]

    return _ingest_series_pair(tmp_path, "alpha", posting(1), posting(2))


def options(body):
    select_html = re.search(
        r'<select\b[^>]*name="pos"[^>]*>(.*?)</select>', body, re.DOTALL
    )
    assert select_html is not None, "native position selector is missing"
    return re.findall(
        r'<option\b[^>]*value="([^"]+)"[^>]*>(.*?)</option>', select_html[1], re.DOTALL
    )


def test_complete_hundred_patch_series_has_every_position_without_body_fanout(
    client,
    tmp_path,
    monkeypatch,
):
    from mimir.web.routes import series_diff as route

    key = series_pair(tmp_path, 100)
    with SessionLocal() as session:
        rows = session.scalars(
            select(Article).where(Article.patch_series_key == key)
        ).all()
        assert len(rows) == 202
        assert {a.patch_series_position for a in rows} == set(range(101))
    reads = []
    diffs = []
    read = route.read_first_readable
    compute = route.compute_revision_diff
    monkeypatch.setattr(
        route, "read_first_readable", lambda *args: (reads.append(1), read(*args))[1]
    )
    monkeypatch.setattr(
        route,
        "compute_revision_diff",
        lambda *args, **kw: (diffs.append(1), compute(*args, **kw))[1],
    )
    url = f"/alpha/series/{key}/diff?from=v1&to=v2&pos=50"
    response = client.get(url)
    assert response.status_code == 200
    choices = options(response.text)
    assert [value for value, _ in choices] == ["cover", *map(str, range(1, 101))]
    assert len(reads) == 2
    assert len(diffs) == 1
    assert client.get(url).status_code == 200
    assert len(reads) == 2
    assert len(diffs) == 1
    assert html.unescape(choices[50][1]).strip() == "Patch 50"


def test_changed_fallback_counterpart_invalidates_the_cached_body(client, tmp_path):
    key = series_pair(tmp_path)
    with SessionLocal() as session:
        patches = session.scalars(
            select(Article).where(Article.patch_series_position > 0)
        ).all()
        for article in patches:
            article.patch_series_key = None
            article.patch_series_version = None
            article.patch_series_position = None
        session.commit()
    url = f"/alpha/series/{key}/diff?from=v1&to=v2&pos=1"
    first = client.get(url)
    assert first.status_code == 200
    assert "body 2 position 1" in first.text
    with SessionLocal() as session:
        old = session.scalar(select(Article).where(Article.message_id == "v2-1@nav"))
        new = session.scalar(select(Article).where(Article.message_id == "v2-2@nav"))
        old.subject = "[PATCH v2 1/3] unrelated"
        new.subject = "[PATCH v2 2/3] change 1"
        session.commit()
    second = client.get(url)
    assert second.status_code == 200
    assert "body 2 position 2" in second.text
    assert "body 2 position 1" not in second.text


def navigation(session, key, position=0, older="v1", newer="v2", head=None):
    from mimir.models import Inbox
    from mimir.web.series_diff_navigation import load_series_diff_navigation

    inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
    return load_series_diff_navigation(
        session, inbox, key, head, older, newer, position
    )


def test_navigation_queries_stay_bounded_and_seek_the_series_index(client, tmp_path):
    from sqlalchemy import event

    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    key = series_pair(tmp_path, 100)
    with planner_stats(SessionLocal, PRODUCTION_STATS), SessionLocal() as session:
        plan = plan_of(
            session,
            lambda: navigation(session, key),
            "WHERE articles.patch_series_key =",
        )
        assert "SEARCH articles USING INDEX ix_articles_patch_series_key" in plan
        assert "SCAN articles" not in plan
        assert "SEARCH article_files USING COVERING INDEX" in plan
    with SessionLocal() as session:
        statements = []
        engine = session.get_bind()

        def capture(conn, cursor, statement, params, context, many):
            if statement.startswith("SELECT"):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            nav = navigation(session, key)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert len(nav.positions) == 101
        assert (
            len(statements) == 10
        )  # inbox + nine metadata SELECTs, independent of positions
        assert nav.extent == 100
        assert nav.previous_url is None
        assert nav.next_url == nav.positions[1].url


def test_fallback_navigation_matches_renumbered_patches_and_omits_missing(
    client, tmp_path
):
    key = series_pair(tmp_path)
    with SessionLocal() as session:
        for article in session.scalars(
            select(Article).where(Article.patch_series_position > 0)
        ):
            article.patch_series_key = None
            article.patch_series_position = None
        moved = session.scalar(select(Article).where(Article.message_id == "v2-1@nav"))
        moved.subject = "[PATCH v2 7/7] change 1"
        missing = session.scalar(
            select(Article).where(Article.message_id == "v2-2@nav")
        )
        missing.subject = "[PATCH v2 2/7] unrelated"
        session.commit()
    with SessionLocal() as session:
        nav = navigation(session, key, 1)
        assert nav.indexed is False
        assert nav.to_article.message_id == "v2-1@nav"
        assert [link.position for link in nav.positions] == [0, 1, 3]
        assert nav.extent == 7
        assert nav.next_url == nav.positions[-1].url
    for pos, expected in [(1, 200), (2, 404), (3, 200), (7, 404)]:
        assert (
            client.get(f"/alpha/series/{key}/diff?from=v1&to=v2&pos={pos}").status_code
            == expected
        )


def test_sparse_huge_ordinals_do_not_expand_the_navigation(client, tmp_path):
    key = series_pair(tmp_path, 1)
    huge = 2**62
    with SessionLocal() as session:
        for article in session.scalars(
            select(Article).where(Article.patch_series_position == 1)
        ):
            article.patch_series_position = huge
            article.thread_parent = None
        session.commit()
    with SessionLocal() as session:
        nav = navigation(session, key, huge)
        assert [link.position for link in nav.positions] == [0, huge]
        assert nav.extent == huge
        assert nav.next_url is None
        assert nav.previous_url == nav.positions[0].url


def test_head_and_exact_versions_survive_native_get_form(client, tmp_path):
    from html.parser import HTMLParser
    from urllib.parse import urlencode

    key = series_pair(tmp_path, 1)
    with SessionLocal() as session:
        rows = session.scalars(
            select(Article).where(Article.patch_series_key == key)
        ).all()
        head = min(a.id for a in rows)
        for article in rows:
            article.patch_series_head_id = head
            if article.patch_series_version == "v1":
                article.patch_series_version = "RFC v1"
        session.commit()
    response = client.get(
        f"/alpha/series/{key}/diff?"
        + urlencode({"from": "RFC v1", "to": "v2", "pos": "cover", "s": head})
    )
    assert response.status_code == 200
    tags = []

    class Tags(HTMLParser):
        def handle_starttag(self, tag, attrs):
            tags.append((tag, dict(attrs)))

    Tags().feed(response.text)
    form = next(attrs for tag, attrs in tags if tag == "form")
    assert form["method"] == "get"
    hidden = {
        attrs["name"]: attrs["value"]
        for tag, attrs in tags
        if tag == "input" and attrs.get("type") == "hidden"
    }
    assert hidden == {"from": "RFC v1", "to": "v2", "s": str(head)}
    submitted = client.get(form["action"] + "?" + urlencode({**hidden, "pos": "1"}))
    assert submitted.status_code == 200
    links = [
        attrs["data-url"]
        for tag, attrs in tags
        if tag == "option" and "data-url" in attrs
    ]
    assert len(links) == 2
    assert all(
        "from=RFC+v1&to=v2&pos=" in url and url.endswith(f"&s={head}") for url in links
    )


def test_adjacent_pairs_use_current_position_or_explicit_cover(client, tmp_path):
    from unittest.mock import patch

    from mimir.web.series_diff_navigation import _SeriesComparisons

    key = series_pair(tmp_path, 2)
    with SessionLocal() as session:
        cover = session.scalar(
            select(Article).where(Article.message_id == "v2-cover@nav")
        )
        third = Article(
            message_id="v3-cover@nav",
            subject="[PATCH v3 0/2] navigation series",
            patch_series_key=key,
            patch_series_version="v3",
            patch_series_position=0,
            date=cover.date,
        )
        session.add(third)
        session.commit()
    calls = []
    resolve = _SeriesComparisons.resolve

    def recording(self, older, newer, position):
        calls.append((older, newer, position))
        return resolve(self, older, newer, position)

    with (
        patch.object(_SeriesComparisons, "resolve", recording),
        SessionLocal() as session,
    ):
        nav = navigation(session, key, 2)
    assert [(p.from_version, p.to_version, p.position) for p in nav.pairs] == [
        ("v2", "v3", 0)
    ]
    assert [pos for old, new, pos in calls if (old, new) == ("v2", "v3")] == [2, 0]
    page = client.get(f"/alpha/series/{key}/diff?from=v1&to=v2&pos=2")
    assert re.search(r">v2→v3 \(cover letter\)</a>", page.text)


def test_ambiguous_fallback_has_no_link_and_still_returns_404(client, tmp_path):
    from mimir.models import ArticleFile

    key = series_pair(tmp_path)
    with SessionLocal() as session:
        for article in session.scalars(
            select(Article).where(Article.patch_series_position > 0)
        ):
            article.patch_series_key = None
            article.patch_series_position = None
            if article.message_id in {"v1-2@nav", "v2-2@nav", "v2-3@nav"}:
                session.add(ArticleFile(article_id=article.id, path="shared.c"))
            if article.message_id == "v2-3@nav":
                article.subject = "[PATCH v2 3/3] change 2"
        session.commit()
    with SessionLocal() as session:
        nav = navigation(session, key)
        assert [link.position for link in nav.positions] == [0, 1]
    response = client.get(f"/alpha/series/{key}/diff?from=v1&to=v2&pos=2")
    assert response.status_code == 404
    assert "Couldn't pick" in html.unescape(response.text)


def test_navigation_and_shared_builder_and_destination_canonical_agree(
    client, tmp_path
):
    from mimir.web.urls import _series_diff_url
    from tests.test_routes.test_series_diff import _canonical, _file_in_beta

    key = series_pair(tmp_path)
    _file_in_beta(tmp_path, ["v2-2@nav"])
    with SessionLocal() as session:
        from mimir.models import Inbox

        beta = session.scalar(select(Inbox).where(Inbox.name == "beta"))
        patch = session.scalar(select(Article).where(Article.message_id == "v2-2@nav"))
        patch.canonical_inbox_id = beta.id
        session.commit()
    with SessionLocal() as session:
        nav = navigation(session, key)
        for link in nav.positions:
            winner = session.scalar(
                select(Article).where(
                    Article.patch_series_key == key,
                    Article.patch_series_version == "v2",
                    Article.patch_series_position == link.position,
                )
            )
            expected = _series_diff_url(
                winner,
                key,
                "v1",
                "v2",
                link.position,
                head_id=None,
                fallback_inbox="alpha",
            )
            assert link.url == expected
    assert nav.positions[2].url.startswith("/beta/")
    for link in nav.positions:
        response = client.get(link.url)
        assert response.status_code == 200
        assert _canonical(response.text) == "http://localhost" + link.url


def test_warm_diff_navigation_observes_missing_counterparts(
    client, tmp_path, monkeypatch
):
    from mimir.web.routes import series_diff as route

    key = series_pair(tmp_path)
    url = f"/alpha/series/{key}/diff?from=v1&to=v2&pos=1"
    assert len(options(client.get(url).text)) == 4
    with SessionLocal() as session:
        article = session.scalar(
            select(Article).where(Article.message_id == "v2-3@nav")
        )
        article.patch_series_key = None
        article.thread_parent = None
        session.commit()

    def no_body(*args, **kwargs):
        raise AssertionError("warm selected diff read a body")

    monkeypatch.setattr(route, "read_first_readable", no_body)
    response = client.get(url)
    assert response.status_code == 200
    assert [value for value, _ in options(response.text)] == ["cover", "1", "2"]


def test_old_render_cache_entry_cannot_supply_the_new_line_markup(client, tmp_path):
    from mimir import cache
    from mimir.patch_revisions import RevisionDiff

    key = series_pair(tmp_path, 1)
    with SessionLocal() as session:
        left = session.scalar(select(Article).where(Article.message_id == "v1-1@nav"))
        right = session.scalar(select(Article).where(Article.message_id == "v2-1@nav"))
        old_key = f"revision_diff:alpha:v1:v2:1:{left.id}:{right.id}"
        cached = RevisionDiff(
            left.id,
            right.id,
            "v1",
            "v2",
            "/old/from",
            "/old/to",
            1,
            "old-format-marker",
            False,
        )
        cache.set(old_key, cached, 86400)
        assert cache.get(old_key).diff_html == "old-format-marker"
    response = client.get(f"/alpha/series/{key}/diff?from=v1&to=v2&pos=1")
    assert response.status_code == 200
    assert "old-format-marker" not in response.text
    assert 'class="diff-line gi"' in response.text


def test_fallback_discovery_seeks_thread_parent_under_production_statistics(
    client, tmp_path
):
    from mimir.models import Inbox
    from mimir.patch_revisions import resolve_series_patches
    from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats

    series_pair(tmp_path, 100)
    with planner_stats(SessionLocal, PRODUCTION_STATS), SessionLocal() as session:
        inbox = session.scalar(select(Inbox).where(Inbox.name == "alpha"))
        cover = session.scalar(
            select(Article).where(Article.message_id == "v1-cover@nav")
        )
        plan = plan_of(
            session,
            lambda: resolve_series_patches(session, inbox, cover),
            "JOIN article_lists",
        )
        assert "SEARCH articles USING INDEX ix_articles_thread_parent" in plan
        assert (
            "SEARCH article_lists USING COVERING INDEX sqlite_autoindex_article_lists_1"
            in plan
        )
        assert "SCAN articles" not in plan
        assert "SCAN article_lists" not in plan


def test_diff_navigation_uses_full_response_htmx_and_restorable_metadata(
    client, tmp_path
):
    from html.parser import HTMLParser

    key = series_pair(tmp_path)
    url = f"/alpha/series/{key}/diff?from=v1&to=v2&pos=1"
    page = client.get(url)
    boosted = client.get(url, headers={"HX-Request": "true"})
    assert page.status_code == boosted.status_code == 200
    assert "<!doctype html>" in boosted.text
    assert "HX-Request" not in boosted.headers.get("Vary", "")
    tags = []

    class Tags(HTMLParser):
        def handle_starttag(self, tag, attrs):
            tags.append((tag, dict(attrs)))

    Tags().feed(page.text)
    article = next(
        attrs
        for tag, attrs in tags
        if tag == "article" and attrs.get("id") == "series-diff"
    )
    assert article["hx-target:inherited"] == article["hx-select:inherited"] == "main"
    assert article["hx-push-url:inherited"] == "true"
    assert article["data-page-title"] == html.unescape(
        re.search(r"<title>(.*?)</title>", page.text)[1]
    )
    assert article["data-canonical"] == "http://localhost" + url
    assert "patch 1" in article["data-page-description"]
    boosted_nav = [
        attrs
        for tag, attrs in tags
        if tag == "nav" and attrs.get("hx-boost:inherited") == "true"
    ]
    assert (
        len(boosted_nav) == 2
    )  # position pager and overview, no other pair in fixture
    root = next(attrs for tag, attrs in tags if tag == "html")
    assert "data-diff-prev" not in root and "data-diff-next" not in root
