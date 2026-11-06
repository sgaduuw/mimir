"""If-None-Match uses weak comparison without merging representations (#1104)."""

from itertools import product

import pytest

from mimir.merge_window import MERGE_WINDOW_PATH
from tests.test_routes._helpers import _ingest_one_article


@pytest.fixture
def etag_routes(client, tmp_path):
    article_id, message = _ingest_one_article(
        tmp_path, "alpha", "weak-etag@example.com", body=b"a real message body\n"
    )
    return {
        "message": message,
        "thread": message + "/t",
        "body": f"/api/alpha/body/{article_id}",
        "merge-window": MERGE_WINDOW_PATH,
    }


@pytest.mark.parametrize("route", ["message", "thread", "body", "merge-window"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    ("validator", "expected"),
    [
        ("weak", 304),
        ("strong", 304),
        ("wildcard", 304),
        ("stale-weak", 200),
        ("stale-strong", 200),
        ("mixed", 304),
    ],
)
def test_conditional_routes_compare_weakly(
    client, etag_routes, route, method, validator, expected
):
    url = etag_routes[route]
    first = client.get(url)
    assert first.status_code == 200 and first.data
    etag, weak = first.get_etag()
    assert etag and not weak, "the origin must actually supply a strong validator"
    quoted = first.headers["ETag"]
    headers = {
        "weak": "W/" + quoted,
        "strong": quoted,
        "wildcard": "*",
        "stale-weak": 'W/"unrelated-validator"',
        "stale-strong": '"unrelated-validator"',
        "mixed": '"unrelated-validator", W/' + quoted,
    }
    response = client.open(
        url, method=method, headers={"If-None-Match": headers[validator]}
    )
    assert response.status_code == expected
    assert response.headers["ETag"] == quoted
    if expected == 304 or method == "HEAD":
        assert response.data == b""
    else:
        assert response.data


@pytest.mark.parametrize("route", ["message", "thread"])
@pytest.mark.parametrize(
    ("source", "target"), tuple(product(["full", "hx", "md"], repeat=2))
)
def test_weak_tags_still_select_the_same_representation(
    client, etag_routes, route, source, target
):
    url = etag_routes[route]
    variants = {
        "full": (url, {}),
        "hx": (url, {"HX-Request": "true"}),
        "md": (url + ".md", {}),
    }
    responses = {
        name: client.get(path, headers=headers)
        for name, (path, headers) in variants.items()
    }
    assert all(response.status_code == 200 for response in responses.values())
    tags = {name: response.headers["ETag"] for name, response in responses.items()}
    assert len(set(tags.values())) == 3, "the fixture must expose distinct validators"
    assert b"<html" in responses["full"].data
    assert b"<html" not in responses["hx"].data
    assert responses["md"].mimetype == "text/markdown"

    path, headers = variants[target]
    response = client.get(
        path, headers={**headers, "If-None-Match": "W/" + tags[source]}
    )
    assert response.status_code == (304 if source == target else 200)
    assert response.headers["ETag"] == tags[target]
