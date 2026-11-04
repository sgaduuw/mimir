"""Incomplete comparisons land on a real conversation of the exact series."""

import pytest
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleFile, ArticleList, Inbox
from mimir.web.urls import _advertised_urls_for
from tests.test_routes._helpers import _ingest_series_pair


def _pair(tmp_path):
    return _ingest_series_pair(
        tmp_path,
        "alpha",
        [
            (
                "first@incomplete",
                "[PATCH 0/1] incomplete series",
                None,
                b"first cover\n",
                "A <a@x>",
            )
        ],
        [
            (
                "second@incomplete",
                "[PATCH v2 0/1] incomplete series",
                None,
                b"second cover\n",
                "A <a@x>",
            )
        ],
    )


def _destination(message_id):
    with SessionLocal() as session:
        article = session.scalars(
            select(Article).where(Article.message_id == message_id)
        ).one()
        return _advertised_urls_for(session, [article])[article.id]


@pytest.mark.parametrize(
    "query,message_id",
    [
        ("", "first@incomplete"),
        ("?from=v1", "first@incomplete"),
        ("?to=v2", "second@incomplete"),
        ("?from=v1&to=v2", "second@incomplete"),
        ("?from=v1&pos=cover", "first@incomplete"),
        ("?to=v2&pos=7", "second@incomplete"),
        ("?from=%20&to=v2&pos=0", "second@incomplete"),
        ("?from=v1&to=v2&pos=%20", "second@incomplete"),
    ],
)
def test_incomplete_comparison_redirects_to_existing_conversation(
    client, tmp_path, query, message_id
):
    key = _pair(tmp_path)
    target = _destination(message_id)
    assert client.get(target).status_code == 200
    response = client.get(f"/alpha/series/{key}/diff{query}")
    assert response.status_code == 301
    assert response.headers["Location"] == target


@pytest.mark.parametrize(
    "query",
    [
        "?from=v1&to=v1",
        "?from=v9",
        "?from=v1&to=v9",
        "?to=v9&pos=cover",
        "?pos=-1",
        "?pos=garbage",
        "?pos=9223372036854775808",
        "?s=",
        "?s=0",
        "?s=-1",
        "?s=999999",
        "?s=9223372036854775808",
    ],
)
def test_incomplete_comparison_does_not_launder_invalid_inputs(client, tmp_path, query):
    key = _pair(tmp_path)
    assert client.get(f"/alpha/series/{key}/diff{query}").status_code == 404


def test_incomplete_comparison_checks_inbox_and_series(client, tmp_path):
    key = _pair(tmp_path)
    assert client.get(f"/missing/series/{key}/diff").status_code == 404
    assert client.get("/alpha/series/no-such-key/diff").status_code == 404


def test_incomplete_redirect_is_metadata_only_and_has_no_diff_validators(
    client, tmp_path, monkeypatch
):
    from unittest.mock import Mock

    from mimir.web import series_diff_navigation
    from mimir.web.routes import series_diff

    key = _pair(tmp_path)
    reads = Mock(side_effect=AssertionError("redirect read a body"))
    fallback = Mock(side_effect=AssertionError("redirect matched patches"))
    compute = Mock(side_effect=AssertionError("redirect computed a diff"))
    monkeypatch.setattr(series_diff, "read_first_readable", reads)
    monkeypatch.setattr(series_diff, "compute_revision_diff", compute)
    monkeypatch.setattr(series_diff_navigation, "resolve_series_patches", fallback)
    response = client.get(f"/alpha/series/{key}/diff")
    assert response.status_code == 301
    reads.assert_not_called()
    compute.assert_not_called()
    fallback.assert_not_called()
    assert "Content-Security-Policy" in response.headers
    assert "ETag" not in response.headers
    assert "Link" not in response.headers
    assert "max-age=" not in response.headers.get("Cache-Control", "")


def test_single_cover_and_unmaterialised_thread_keep_a_real_destination(
    client, tmp_path
):
    key = _pair(tmp_path)
    with SessionLocal() as session:
        covers = session.scalars(
            select(Article).where(
                Article.message_id.in_(["first@incomplete", "second@incomplete"])
            )
        ).all()
        first, second = sorted(covers, key=lambda a: a.id)
        second.patch_series_key = "another-key"
        for link in first.lists:
            link.thread_root_id = None
        session.commit()
    target = _destination("first@incomplete")
    assert not target.endswith("/t"), "fixture must exercise message fallback"
    response = client.get(f"/alpha/series/{key}/diff")
    assert response.status_code == 301
    assert response.headers["Location"] == target
    assert client.get(target).status_code == 200


def test_incomplete_redirect_separates_heads_sharing_a_key(client, tmp_path):
    key = _pair(tmp_path)
    with SessionLocal() as session:
        second = session.scalars(
            select(Article).where(Article.message_id == "second@incomplete")
        ).one()
        second.patch_series_head_id = second.id
        head = second.id
        session.commit()
    first_target = _destination("first@incomplete")
    second_target = _destination("second@incomplete")
    assert first_target != second_target
    assert client.get(f"/alpha/series/{key}/diff").headers["Location"] == first_target
    assert (
        client.get(f"/alpha/series/{key}/diff?s={head}").headers["Location"]
        == second_target
    )
    assert client.get(f"/alpha/series/{key}/diff?to=v2").status_code == 404
    assert client.get(f"/alpha/series/{key}/diff?to=v1&s={head}").status_code == 404


def test_duplicate_cover_uses_the_shared_slot_winner(client, tmp_path):
    key = _pair(tmp_path)
    with SessionLocal() as session:
        original = session.scalars(
            select(Article).where(Article.message_id == "first@incomplete")
        ).one()
        link = original.lists[0]
        duplicate = Article(
            message_id="resend@incomplete",
            subject=original.subject,
            author=original.author,
            date=original.date,
            patch_series_key=key,
            patch_series_version="v1",
            patch_series_position=0,
        )
        session.add(duplicate)
        session.flush()
        session.add(
            ArticleList(
                article_id=duplicate.id,
                inbox_id=link.inbox_id,
                epoch=link.epoch,
                commit_sha=link.commit_sha,
                thread_root_id=duplicate.id,
            )
        )
        session.add(ArticleFile(article_id=duplicate.id, path="change.c"))
        assert duplicate.id > original.id
        assert not original.files
        session.commit()
    expected = _destination("resend@incomplete")
    response = client.get(f"/alpha/series/{key}/diff?from=v1")
    assert response.status_code == 301
    assert response.headers["Location"] == expected
    assert client.get(expected).status_code == 200


def test_redirect_uses_canonical_crosspost_and_page_holding_later_cover(
    client, tmp_path, monkeypatch
):
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 1)
    key = _pair(tmp_path)
    with SessionLocal() as session:
        first = session.scalars(
            select(Article).where(Article.message_id == "first@incomplete")
        ).one()
        second = session.scalars(
            select(Article).where(Article.message_id == "second@incomplete")
        ).one()
        beta = session.scalars(select(Inbox).where(Inbox.name == "beta")).one()
        beta.mirror_path = str(tmp_path)
        second.thread_parent = first.message_id
        for article in (first, second):
            source = article.lists[0]
            source.thread_root_id = first.id
            article.canonical_inbox_id = beta.id
            session.add(
                ArticleList(
                    article_id=article.id,
                    inbox_id=beta.id,
                    epoch=source.epoch,
                    commit_sha=source.commit_sha,
                    thread_root_id=first.id,
                )
            )
        session.commit()
    target = _destination("second@incomplete")
    assert target.startswith("/beta/") and target.endswith("/t/2"), target
    response = client.get(f"/alpha/series/{key}/diff?to=v2")
    assert response.status_code == 301
    assert response.headers["Location"] == target
    final = client.get(target, follow_redirects=True)
    assert final.status_code == 200
    assert "second cover" in final.get_data(as_text=True)


def test_real_sitemap_comparisons_remain_allowed_including_split_heads(client):
    from datetime import UTC, datetime
    from urllib.parse import urlsplit
    from xml.etree import ElementTree

    from mimir import robots
    from mimir.seo.sitemaps import series_diff_sitemap_xml
    from tests.test_robots import _blocked
    from tests.test_routes.test_series_diff_sitemap import _seed_covers

    key = "a" * 40
    _seed_covers(
        key,
        [
            ("v1", datetime(2024, 1, 1, tzinfo=UTC)),
            ("v2", datetime(2024, 1, 2, tzinfo=UTC)),
            ("v1", datetime(2024, 2, 1, tzinfo=UTC)),
            ("v2", datetime(2024, 2, 2, tzinfo=UTC)),
        ],
    )
    with SessionLocal() as session:
        rows = session.scalars(
            select(Article).where(Article.patch_series_key == key).order_by(Article.id)
        ).all()
        for article in rows[2:]:
            article.patch_series_head_id = rows[2].id
        session.commit()
        xml = series_diff_sitemap_xml(
            session, "a", "https://example.com", force=True
        ).body
    urls = [node.text for node in ElementTree.fromstring(xml).findall("{*}url/{*}loc")]
    assert len(urls) == 2
    assert any("&s=" in url for url in urls)
    assert any("&s=" not in url for url in urls)
    for url in urls:
        parsed = urlsplit(url)
        assert _blocked("/*/series/*/diff$", parsed.path)
        assert not any(
            _blocked(pattern, parsed.path + "?" + parsed.query)
            for pattern in robots._DEFAULT_STAR_DISALLOW
        )


def test_incomplete_comparison_rejects_a_cover_without_a_destination(client, tmp_path):
    key = _pair(tmp_path)
    with SessionLocal() as session:
        first = session.scalars(
            select(Article).where(Article.message_id == "first@incomplete")
        ).one()
        first.date = None
        session.commit()
    assert client.get(f"/alpha/series/{key}/diff?from=v1").status_code == 404
