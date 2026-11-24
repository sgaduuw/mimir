"""Feed and archive validators hash served bytes without changing cache deadlines."""

from xml.etree import ElementTree as ET

import pytest

from mimir.config import settings
from tests.test_routes.test_maintainer_feeds import ADDRESS, NS, _maintainer, _message

PATHS = [
    "/alpha/feed.atom",
    "/alpha/author/foo@bar.org/feed.atom",
    "/maintainers/foo@bar.org/feed.atom",
    "/alpha/2024/",
    "/alpha/2024/06/",
    "/alpha/2024/01/",
]


@pytest.mark.parametrize("path", PATHS)
def test_feed_archive_etag_keeps_cache_policy(client, session, frozen_clock, path):
    _maintainer(session)
    _message(session, "etag-entry", ADDRESS)
    first = client.get(path)
    assert first.status_code == 200
    assert first.data
    tag = first.headers["ETag"]
    assert not tag.startswith("W/")
    assert "Last-Modified" not in first.headers
    is_feed = path.endswith(".atom")
    assert first.cache_control.max_age == (300 if is_feed else 600)
    edge = first.headers.get("Cloudflare-CDN-Cache-Control")
    expected_edge = {
        "/alpha/2024/06/": "max-age=600",
        "/alpha/2024/01/": "max-age=604800",
    }
    assert edge == expected_edge.get(path)
    frozen_clock.tick(5)
    for supplied in (tag, "W/" + tag):
        response = client.get(path, headers={"If-None-Match": supplied})
        assert response.status_code == 304
        assert response.data == b""
        assert response.headers["ETag"] == tag
        assert "Last-Modified" not in response.headers
        assert response.cache_control.public
        assert response.cache_control.max_age == (295 if is_feed else 600)
        assert response.headers.get("Cloudflare-CDN-Cache-Control") == edge
    future = "Wed, 01 Jan 2031 00:00:00 GMT"
    for headers in (
        {"If-None-Match": '"different"'},
        {"If-Modified-Since": future},
        {"If-None-Match": '"different"', "If-Modified-Since": future},
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.data == first.data
        assert response.headers["ETag"] == tag
    head = client.head(path)
    assert head.status_code == 200 and head.data == b""
    assert head.headers["ETag"] == tag
    assert int(head.headers["Content-Length"]) == len(first.data)
    response = client.head(path, headers={"If-None-Match": "W/" + tag})
    assert response.status_code == 304 and response.data == b""
    assert response.headers["ETag"] == tag


@pytest.mark.parametrize("path", PATHS)
def test_feed_archive_etag_changes_with_rendered_metadata(
    client, session, frozen_clock, monkeypatch, path
):
    _maintainer(session)
    _message(session, "unchanged-entry", ADDRESS)
    first = client.get(path)
    assert first.status_code == 200
    monkeypatch.setattr(settings, "site_name", "Changed site name")
    changed = client.get(path, headers={"If-None-Match": first.headers["ETag"]})
    assert changed.status_code == 200
    assert changed.data != first.data
    assert changed.headers["ETag"] != first.headers["ETag"]


def test_empty_feed_etag_tracks_existing_dynamic_updated_time(client, frozen_clock):
    path = "/alpha/author/no-such-author/feed.atom"
    first = client.get(path)
    assert first.status_code == 200
    before = ET.fromstring(first.data)
    assert before.findall("a:entry", NS) == []
    frozen_clock.tick(2)
    changed = client.get(path, headers={"If-None-Match": first.headers["ETag"]})
    assert changed.status_code == 200
    after = ET.fromstring(changed.data)
    assert after.findall("a:entry", NS) == []
    assert before.findtext("a:updated", namespaces=NS) != after.findtext(
        "a:updated", namespaces=NS
    )
    assert changed.headers["ETag"] != first.headers["ETag"]
