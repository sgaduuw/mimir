"""Status-code smoke tests for every public route.

Catches the routine "did I break the URL routing or break a template
unrelated to my actual change" regression with one cheap test per
endpoint. Hits the running DB — assumes at least one configured
inbox exists. Doesn't validate response bodies beyond a token check
where it pins something a refactor would silently break (404 on
unknown inbox, 404 on date mismatch on the message URL, etc.).
"""
import pytest

from mimir import create_app


@pytest.fixture(scope="module")
def client():
    return create_app().test_client()


@pytest.fixture(scope="module")
def inbox_name():
    """Pick the first configured inbox in alphabetical order. The
    suite needs *some* inbox to hit; if none is bootstrapped, skip."""
    from mimir.inboxes import inbox_names
    names = inbox_names()
    if not names:
        pytest.skip("no inboxes bootstrapped; ingest-test path can't run")
    return names[0]


# Endpoints that don't depend on a configured inbox.
def test_meta_index(client):
    assert client.get("/").status_code == 200


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "no-store"


def test_readyz(client):
    assert client.get("/readyz").status_code == 200


def test_unknown_inbox_404(client):
    assert client.get("/nonexistent/").status_code == 404
    assert client.get("/nonexistent/today").status_code == 404
    assert client.get("/nonexistent/2024/").status_code == 404
    assert client.get("/nonexistent/2024/05/").status_code == 404
    assert client.get("/nonexistent/feed.atom").status_code == 404
    assert client.get("/nonexistent/search?q=foo").status_code == 404


# Endpoints scoped to a real inbox.
@pytest.mark.parametrize("path", [
    "/",
    "/today",
    "/yesterday",
    "/2024/",
    "/2024/05/",
    "/search",
    "/search?q=Linux",
    "/feed.atom",
])
def test_inbox_scoped_routes_200(client, inbox_name, path):
    assert client.get(f"/{inbox_name}{path}").status_code == 200


def test_year_out_of_range_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/1990/").status_code == 404
    # _max_archive_year() = current_year + 1; pick something solidly above.
    assert client.get(f"/{inbox_name}/2999/").status_code == 404


def test_month_out_of_range_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/2024/0/").status_code == 404
    assert client.get(f"/{inbox_name}/2024/13/").status_code == 404


def test_search_too_short(client, inbox_name):
    """Single-char query: server-side min-length check kicks in;
    response is 200 but renders the 'type at least N characters'
    notice rather than running a search."""
    r = client.get(f"/{inbox_name}/search?q=x")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "characters" in body.lower()


def test_author_view_too_short_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/author/x").status_code == 404


def test_author_feed_too_short_404(client, inbox_name):
    assert client.get(f"/{inbox_name}/author/x/feed.atom").status_code == 404


def test_request_id_round_trip(client):
    """X-Request-Id from the client should come back on the response;
    if absent, the server mints one."""
    r = client.get("/healthz", headers={"X-Request-Id": "abc-test-id"})
    assert r.headers.get("X-Request-Id") == "abc-test-id"
    r = client.get("/healthz")
    rid = r.headers.get("X-Request-Id")
    assert rid and rid != "abc-test-id"


def test_security_headers_present(client):
    r = client.get("/")
    for h in (
        "Content-Security-Policy",
        "Referrer-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
    ):
        assert r.headers.get(h), f"missing header {h}"


def test_atom_feed_well_formed(client, inbox_name):
    import xml.etree.ElementTree as ET

    r = client.get(f"/{inbox_name}/feed.atom")
    assert r.status_code == 200
    assert r.mimetype.startswith("application/atom+xml")
    root = ET.fromstring(r.get_data())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    # Required Atom fields on the feed element.
    assert root.find("a:id", ns) is not None
    assert root.find("a:title", ns) is not None
    assert root.find("a:updated", ns) is not None


# robots.txt / security.txt / sitemap.xml


def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert r.mimetype == "text/plain"
    body = r.get_data(as_text=True)
    assert "User-agent: *" in body
    assert "Sitemap:" in body
    assert "/sitemap.xml" in body


def test_security_txt_404_when_unconfigured(client, monkeypatch):
    from mimir.config import settings

    monkeypatch.setattr(settings, "security_contact", None)
    assert client.get("/security.txt").status_code == 404
    assert client.get("/.well-known/security.txt").status_code == 404


def test_security_txt_when_configured(client, monkeypatch):
    from mimir.config import settings

    monkeypatch.setattr(settings, "security_contact", "mailto:test@example.com")
    monkeypatch.setattr(settings, "security_policy_url", None)
    monkeypatch.setattr(settings, "security_encryption_url", None)
    for url in ("/security.txt", "/.well-known/security.txt"):
        r = client.get(url)
        assert r.status_code == 200
        assert r.mimetype == "text/plain"
        body = r.get_data(as_text=True)
        assert "Contact: mailto:test@example.com" in body
        # Expires is dynamic; just confirm the field is present.
        assert "Expires:" in body
        assert "Preferred-Languages: en" in body


def test_security_txt_optional_fields_emitted_when_set(client, monkeypatch):
    from mimir.config import settings

    monkeypatch.setattr(settings, "security_contact", "mailto:test@example.com")
    monkeypatch.setattr(settings, "security_policy_url", "https://example.com/policy")
    monkeypatch.setattr(settings, "security_encryption_url", "https://example.com/pgp.asc")
    body = client.get("/security.txt").get_data(as_text=True)
    assert "Policy: https://example.com/policy" in body
    assert "Encryption: https://example.com/pgp.asc" in body


def test_sitemap_xml(client):
    import xml.etree.ElementTree as ET

    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.mimetype == "application/xml"
    root = ET.fromstring(r.get_data())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = root.findall("s:url", ns)
    # Always at least the meta-index plus per-inbox dashboards.
    assert len(urls) > 0
    locs = {u.find("s:loc", ns).text for u in urls}
    # Meta-index is always present.
    assert any(loc.endswith("/") and loc.count("/") <= 3 for loc in locs)
