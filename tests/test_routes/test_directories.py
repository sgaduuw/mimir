"""Global directories and the plural per-inbox subsystem migration."""

import pytest

from mimir.models import Subsystem, SubsystemMaintainer
from tests._dom import parse


def test_directory_roots_are_pages_even_without_entries(client):
    for path in ("/subsystems/", "/maintainers/"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "public, max-age=300"
        assert (
            "ETag" not in response.headers and "Last-Modified" not in response.headers
        )


def test_unknown_legacy_subsystem_does_not_redirect_to_a_404(client):
    response = client.get("/alpha/subsystem/NO-SUCH-SUBSYSTEM/?x=%2F&x=2")
    assert response.status_code == 404
    assert "Location" not in response.headers


@pytest.fixture
def subsystem(session):
    sub = Subsystem(name="ALPHA DRIVER", status="Maintained")
    session.add(sub)
    session.flush()
    session.add(
        SubsystemMaintainer(
            subsystem_id=sub.id, role="M", name="Alice", address="alice@kernel.org"
        )
    )
    session.commit()
    return sub


@pytest.mark.parametrize("suffix", ["subsystem", "subsystem/", "subsystems"])
def test_subsystem_index_alias_validates_then_preserves_query(client, suffix):
    query = "?sort=most%20active&x=%2F&x=2"
    response = client.get("/alpha/" + suffix + query)
    assert response.status_code == 301
    assert response.headers["Location"] == "/alpha/subsystems/" + query
    assert client.get(response.headers["Location"]).status_code == 200
    assert client.get("/missing/" + suffix + query).status_code == 404


@pytest.mark.parametrize("segment", ["subsystem", "subsystems"])
@pytest.mark.parametrize("name", ["ALPHA%20DRIVER", "alpha%20driver"])
def test_subsystem_alias_uses_actual_destination_and_keeps_query(
    client, subsystem, segment, name
):
    from mimir.subsystems import subsystem_path

    query = "?q=%C3%BC&x=%2F&x=2"
    response = client.get(f"/alpha/{segment}/{name}" + query)
    assert response.status_code == 301
    assert (
        response.headers["Location"] == subsystem_path("alpha", subsystem.name) + query
    )
    assert client.get(response.headers["Location"]).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/missing/subsystem/ALPHA/",
        "/alpha/subsystem/UNKNOWN/",
        "/alpha/subsystems/UNKNOWN/",
        "/alpha/subsystem/ALPHA%0ADRIVER/",
        "/alpha/subsystem/ALPHA%C2%85DRIVER/",
    ],
)
def test_invalid_aliases_never_redirect(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert "Location" not in response.headers


def test_directory_letter_routes_do_not_capture_profiles(client, subsystem):
    assert client.get("/maintainers/alice@kernel.org").status_code == 200
    assert client.get("/maintainers/alice@kernel.org/").status_code == 404
    for path in ("/subsystems", "/maintainers", "/maintainers/a"):
        response = client.get(path)
        assert response.status_code == 301
        assert response.headers["Location"] == path + "/"
        assert client.get(response.headers["Location"]).status_code == 200
    assert client.get("/maintainers/z/").status_code == 404


@pytest.mark.parametrize("available", [False, True])
def test_directory_includes_quiet_entries_and_cache_only_activity(
    client, monkeypatch, available
):
    from mimir.web.routes import directory_pages

    rows = [
        {
            "name": "ALPHA DRIVER",
            "inbox_name": "alpha",
            "path": "/alpha/subsystems/alpha%20driver/",
            "message_count": 8 if available else None,
        },
        {
            "name": "ANOTHER DRIVER",
            "inbox_name": None,
            "path": None,
            "message_count": 0 if available else None,
        },
    ]
    monkeypatch.setattr(
        directory_pages, "subsystem_directory_rows", lambda session: (rows, available)
    )
    response = client.get("/subsystems/a/")
    assert response.status_code == 200
    page = parse(response.get_data(as_text=True))
    rendered = page.find("tbody", id="directory-list").find_all("tr")
    assert len(rendered) == 2
    assert rendered[1].find_all("a") == []
    assert rendered[1].find("td", cls="directory-meta").text() == "not mirrored"
    if available:
        assert [cell.text() for cell in page.find_all("td", cls="listing-count")] == [
            "8",
            "0",
        ]
        assert rendered[1].find("span", cls="count--none").text() == "0"
    else:
        assert page.find_all("td", cls="listing-count") == []
        assert (
            page.find("p", cls="directory-note").text() == "Activity not available yet"
        )


def test_hidden_directory_address_is_absent_from_labels_and_search_metadata(
    client, monkeypatch
):
    from mimir.web import filters
    from mimir.web.routes import directory_pages

    secret = "hidden@private.test"
    rows = [
        {
            "address": secret,
            "name": "Alice " + secret,
            "path": "/maintainers/" + secret,
            "subsystem_count": 1,
            "review_count": 0,
        }
    ]
    monkeypatch.setattr(
        directory_pages, "maintainer_directory_rows", lambda session: (rows, True)
    )
    monkeypatch.setattr(filters, "_is_allowlisted", lambda address: False)
    body = client.get("/maintainers/a/").get_data(as_text=True)
    assert secret not in body
    row = parse(body).find("tbody", id="directory-list").find("tr")
    assert row.find_all("a") == []
    assert row.attrs["data-filter-text"] == "unknown sender"


def test_cold_front_page_still_links_both_directories(client):
    page = parse(client.get("/").get_data(as_text=True))
    links = {a.text(): a.attrs["href"] for a in page.find_all("a")}
    assert links["All subsystems"] == "/subsystems/"
    assert links["All maintainers"] == "/maintainers/"
    assert page.find_all("h2", id="subsystems") == []
    assert page.find_all("h2", id="maintainer-activity") == []
