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
    assert links["all subsystems"] == "/subsystems/"
    assert links["all maintainers"] == "/maintainers/"
    assert "Browse all subsystems · all maintainers" in page.text()
    assert page.find_all("h2", id="subsystems") == []
    assert page.find_all("h2", id="maintainer-activity") == []


@pytest.mark.parametrize("path", ["/subsystems/", "/maintainers/?q=%20%20"])
def test_directory_landing_search_is_usable_without_javascript(client, path):
    page = parse(client.get(path).get_data(as_text=True))
    form = page.find("form", role="search")
    assert form.attrs["method"] == "get"
    assert form.find("input", name="q").attrs["type"] == "search"
    assert form.find("button", type="submit").text() == "Search"
    assert page.find_all("meta", name="robots") == []
    assert page.find_all("tbody", id="directory-list") == []
    assert "Activity not available yet" not in page.text()


@pytest.mark.parametrize("query", ["Kroah", "greg@kernel.org"])
def test_maintainer_search_crosses_letters_using_visible_search_text(
    client, monkeypatch, query
):
    from mimir.web import filters
    from mimir.web.routes import directory_pages

    rows = [
        dict(
            name=name,
            address=address,
            path="/maintainers/" + address,
            subsystem_count=1,
            review_count=0,
        )
        for name, address in [
            ("Alice", "alice@kernel.org"),
            ("Greg Kroah-Hartman", "greg@kernel.org"),
        ]
    ]
    monkeypatch.setattr(
        directory_pages, "maintainer_directory_rows", lambda s: (rows, True)
    )
    monkeypatch.setattr(filters, "_is_allowlisted", lambda address: True)
    response = client.get("/maintainers/", query_string={"q": query})
    page = parse(response.get_data(as_text=True))
    rendered = page.find("tbody", id="directory-list").find_all("tr")
    assert len(rendered) == 1
    assert query.lower() in rendered[0].attrs["data-filter-text"]
    assert page.find("meta", name="robots").attrs["content"] == "noindex"
    assert page.find("link", rel="canonical").attrs["href"].endswith("/maintainers/")


def test_subsystem_search_is_global_bounded_and_not_computed(client, monkeypatch):
    from mimir.web.routes import directory_pages

    rows = [
        dict(name=f"MEMORY THP {n:03}", inbox_name=None, path=None, message_count=None)
        for n in range(205)
    ]
    monkeypatch.setattr(
        directory_pages, "subsystem_directory_rows", lambda s: (rows, False)
    )
    page = parse(client.get("/subsystems/?q=THP").get_data(as_text=True))
    rendered = page.find("tbody", id="directory-list").find_all("tr")
    assert len(rendered) == 200
    assert all("thp" in row.attrs["data-filter-text"] for row in rendered)
    assert "205" in page.text()
    assert "refine" in page.text().lower()
    assert len(rows) == 205


def test_directory_search_does_not_match_hidden_address(client, monkeypatch):
    from mimir.web import filters
    from mimir.web.routes import directory_pages

    secret = "hidden@private.test"
    rows = [
        dict(
            name="Alice " + secret,
            address=secret,
            path="/maintainers/" + secret,
            subsystem_count=1,
            review_count=0,
        )
    ]
    monkeypatch.setattr(
        directory_pages, "maintainer_directory_rows", lambda s: (rows, True)
    )
    monkeypatch.setattr(filters, "_is_allowlisted", lambda address: False)
    page = parse(client.get("/maintainers/?q=hidden").get_data(as_text=True))
    assert page.find_all("table", cls="directory-table") == []


@pytest.mark.parametrize("kind", ["subsystems", "maintainers"])
def test_empty_directory_search_hides_table_and_local_controls(client, kind):
    page = parse(client.get(f"/{kind}/?q=no-such-entry").get_data(as_text=True))
    assert f"No matching {kind}." in page.text()
    assert page.find_all("table", cls="directory-table") == []
    assert page.find_all("div", cls="front-controls") == []
    assert page.find("form", role="search").attrs["action"] == f"/{kind}/"


@pytest.mark.parametrize("kind", ["subsystems", "maintainers"])
@pytest.mark.parametrize("suffix", ["a", "a/", "z/"])
def test_letter_search_redirects_to_global_search_preserving_query(
    client, kind, suffix
):
    query = "?q=foo%20bar&x=%2F&x=2"
    response = client.get(f"/{kind}/{suffix}" + query)
    assert response.status_code == 301
    assert response.headers["Location"] == f"/{kind}/" + query
    result = client.get(response.headers["Location"])
    assert result.status_code == 200
    page = parse(result.get_data(as_text=True))
    assert page.find("input", name="q").attrs["value"] == "foo bar"
    assert "No matching" in page.text()
