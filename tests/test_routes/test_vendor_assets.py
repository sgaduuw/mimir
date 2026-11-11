"""The same pinned frontend bytes are served locally, without a build step."""

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest

from mimir import __version__
from tests._dom import parse


@pytest.mark.parametrize(
    ("tag", "attribute", "path", "mimetypes", "integrity"),
    [
        (
            "script",
            "src",
            "/static/vendor/htmx/4.0.1/htmx.min.js",
            {"text/javascript", "application/javascript"},
            "sha384-Qp9rdC8H7EqOoX+nR0P3RPG2Noji+qdwJlGpfzwQJsfOpp3sGwIdSP9xO3+UlEhn",
        ),
        (
            "link",
            "href",
            "/static/vendor/pico/2.1.1/pico.amber.min.css",
            {"text/css"},
            "sha384-BiacLKDCyG54e35iUPYB3xZg4mZ2my6tLgF44LKpomzgxexC+sAL7aL7f2mxTh7J",
        ),
    ],
)
def test_versioned_assets_keep_the_pinned_bytes_and_static_cache(
    client, tag, attribute, path, mimetypes, integrity
):
    page = parse(client.get("/").get_data(as_text=True))
    matches = [
        node
        for node in page.find_all(tag)
        if urlsplit(node.attrs.get(attribute, "")).path == path
    ]
    assert len(matches) == 1, f"Expected the pinned local asset {path}"
    node = matches[0]
    url = urlsplit(node.attrs[attribute])
    assert not url.scheme and not url.netloc
    assert parse_qs(url.query) == {"v": [__version__]}
    assert node.attrs["integrity"] == integrity
    assert "crossorigin" not in node.attrs
    if tag == "script":
        assert "defer" in node.attrs

    response = client.get(node.attrs[attribute])
    assert response.status_code == 200
    assert response.mimetype in mimetypes
    encoded = base64.b64encode(hashlib.sha384(response.data).digest()).decode()
    assert "sha384-" + encoded == integrity
    assert response.headers["Cache-Control"] == "public, max-age=86400"
    head = client.head(node.attrs[attribute])
    assert head.status_code == 200
    assert head.data == b""
    assert head.content_length == len(response.data)
