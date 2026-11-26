"""Sitemap validators describe the XML representation, not mail dates."""

from datetime import UTC, datetime

import pytest

from tests.test_routes.test_sitemaps import _meta_activity_fixture

_SITEMAPS = [
    "/sitemap.xml",
    "/meta-sitemap.xml",
    "/alpha/sitemap.xml",
    "/sitemap-maintainers.xml",
    "/alpha/2024/sitemap.xml",
    "/alpha/2024/01/sitemap.xml",
]


@pytest.mark.parametrize("path", _SITEMAPS)
def test_sitemap_etag_conditional_requests(client, path):
    first = client.get(path)
    assert first.status_code == 200
    assert first.data
    tag = first.headers["ETag"]
    assert not tag.startswith("W/")
    assert "Last-Modified" not in first.headers
    assert client.get(path).headers["ETag"] == tag
    for supplied in (tag, "W/" + tag, "*", '"another-representation", ' + tag):
        response = client.get(path, headers={"If-None-Match": supplied})
        assert response.status_code == 304
        assert response.data == b""
        assert response.headers["ETag"] == tag
        assert response.headers["Cache-Control"] == first.headers["Cache-Control"]
        assert response.headers.get(
            "Cloudflare-CDN-Cache-Control"
        ) == first.headers.get("Cloudflare-CDN-Cache-Control")
        assert "Last-Modified" not in response.headers
    future = "Wed, 01 Jan 2031 00:00:00 GMT"
    for headers in (
        {"If-None-Match": '"another-representation"'},
        {"If-Modified-Since": future},
        {"If-None-Match": '"another-representation"', "If-Modified-Since": future},
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.data == first.data
        assert response.headers["ETag"] == tag
    head = client.head(path)
    assert head.status_code == 200
    assert head.data == b""
    assert head.headers["ETag"] == tag
    assert int(head.headers["Content-Length"]) == len(first.data)
    conditional_head = client.head(path, headers={"If-None-Match": tag})
    assert conditional_head.status_code == 304
    assert conditional_head.data == b""
    assert conditional_head.headers["ETag"] == tag


def test_meta_etag_changes_on_activity_cache_fill_without_newer_content(
    client, session, monkeypatch, frozen_clock
):
    from mimir import cache
    from mimir.mailmap import rules_version
    from mimir.seo.sitemaps import meta_sitemap_xml
    from mimir.subsystems_dashboard.activity import (
        SubsystemActivity,
        global_subsystem_activity_key,
    )

    sub = _meta_activity_fixture(session, monkeypatch)
    before_payload = meta_sitemap_xml(session, "http://localhost")
    first = client.get("/meta-sitemap.xml")
    assert first.status_code == 200
    assert first.data == before_payload.body.encode()
    tag = first.headers["ETag"]
    cache.set(
        global_subsystem_activity_key(rules_version(session)),
        [
            SubsystemActivity(
                sub.id, sub.name, "beta", 3, datetime(2024, 2, 1, tzinfo=UTC)
            )
        ],
        3600,
    )
    after_payload = meta_sitemap_xml(session, "http://localhost")
    assert after_payload.last_modified == before_payload.last_modified
    assert after_payload.body != before_payload.body
    changed = client.get("/meta-sitemap.xml", headers={"If-None-Match": tag})
    assert changed.status_code == 200
    assert changed.data == after_payload.body.encode()
    assert changed.headers["ETag"] != tag


def test_sitemap_etag_survives_cache_expiry_without_content_change(
    client, frozen_clock
):
    from mimir import cache
    from mimir.seo.sitemaps import SITEMAP_TTL_SEC

    path = "/sitemap-maintainers.xml"
    first = client.get(path)
    assert first.status_code == 200
    tag = first.headers["ETag"]
    assert cache.get("sitemap:maintainers") is not None
    frozen_clock.tick(SITEMAP_TTL_SEC + 1)
    assert cache.get("sitemap:maintainers") is None
    response = client.get(path, headers={"If-None-Match": tag})
    assert response.status_code == 304
    assert response.data == b""
    assert response.headers["ETag"] == tag
    assert cache.get("sitemap:maintainers") is not None
