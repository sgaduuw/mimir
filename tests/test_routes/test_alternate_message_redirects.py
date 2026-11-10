"""Recovery URL shapes reuse the existing lookup and article acceptors."""

from datetime import UTC, date, datetime
from urllib.parse import urlsplit

import pytest

from mimir.web.urls import _msg_path, thread_page_url
from tests._dom import parse
from tests.test_routes._helpers import _ingest_one_article


@pytest.fixture
def alternate_article(client, tmp_path):
    article_id, path = _ingest_one_article(
        tmp_path,
        "alpha",
        "alternate/lookup@example.com",
        subject="alternate redirect target",
    )
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as session:
        article = session.get(Article, article_id)
        article.date = datetime(2026, 5, 1, tzinfo=UTC)
        session.commit()
    path = _msg_path(article_id, date(2026, 5, 1), "alpha")
    assert client.get(path).status_code == 200
    assert client.get(path + "/t").status_code == 200
    return article_id, path


@pytest.mark.parametrize("alias", ["/r/{mid}", "/all/{mid}/", "/all/{mid}"])
def test_lore_alias_reuses_lookup_and_reaches_message(
    client, alternate_article, alias, monkeypatch
):
    _, target = alternate_article

    def no_blob(*args, **kwargs):
        pytest.fail("Message-ID redirect must not read a message body")

    with monkeypatch.context() as patch:
        patch.setattr("mimir.store._blob_bytes", no_blob)
        response = client.get(alias.format(mid="alternate/lookup@example.com"))
    assert response.status_code == 301
    assert response.headers["Location"] == target
    followed = client.get(response.headers["Location"])
    assert followed.status_code == 200
    assert "alternate redirect target" in followed.get_data(as_text=True)


@pytest.mark.parametrize("thread", [False, True], ids=["message", "thread"])
def test_hyphen_alias_reaches_the_existing_acceptor(client, alternate_article, thread):
    article_id, path = alternate_article
    inbox, year, month, _ = path.strip("/").split("/")
    assert month == "05"
    article_date = date(int(year), int(month), 1)
    target = (thread_page_url if thread else _msg_path)(article_id, article_date, inbox)
    alias = f"/{inbox}/{year}-{month}-{article_id}" + ("/t" if thread else "")
    response = client.get(alias)
    assert response.status_code == 301
    assert response.headers["Location"] == target
    followed = client.get(target)
    assert followed.status_code == 200
    assert "alternate redirect target" in followed.get_data(as_text=True)
    canonical = parse(followed.get_data(as_text=True)).find("link", rel="canonical")
    # Message pages still consolidate to their containing thread.
    assert urlsplit(canonical.attrs["href"]).path == thread_page_url(
        article_id, article_date, inbox
    )


@pytest.mark.parametrize("thread", [False, True], ids=["message", "thread"])
@pytest.mark.parametrize(
    "year_month",
    [
        "2026-00",
        "2026-13",
        "0-05",
        "10000-05",
        "99999999999999999999-05",
        "2026-99999999999999999999",
    ],
)
def test_hyphen_alias_rejects_invalid_dates_without_redirect(
    client, year_month, thread
):
    response = client.get(f"/alpha/{year_month}-1" + ("/t" if thread else ""))
    assert response.status_code == 404
    assert "Location" not in response.headers


@pytest.mark.parametrize("thread", [False, True], ids=["message", "thread"])
@pytest.mark.parametrize(
    "encoded_inbox",
    ["alpha%3Ffoo", "alpha%23foo", "alpha%25foo", "alpha%253F", "m%C3%ADmir"],
)
def test_hyphen_redirect_keeps_encoded_inbox_in_path(client, encoded_inbox, thread):
    suffix = "/t" if thread else ""
    response = client.get(f"/{encoded_inbox}/2026-05-1{suffix}")
    assert response.status_code == 301
    target = urlsplit(response.headers["Location"])
    assert target.path == f"/{encoded_inbox}/2026/05/1{suffix}"
    assert target.query == ""
    assert target.fragment == ""


@pytest.mark.parametrize("thread", [False, True], ids=["message", "thread"])
@pytest.mark.parametrize(
    ("hostile", "encoded"),
    [
        # `/\host` is read by browsers as `//host`, so the backslash must stay encoded.
        ("%5Cevil.example", "%5Cevil.example"),
        ("evil.example%5C", "evil.example%5C"),
        ("@evil.example", "%40evil.example"),
        # Browsers strip a tab before parsing, so `/<TAB>\host` is `//host` too.
        ("%09%5Cevil.example", "%09%5Cevil.example"),
        # Routing merges the slashes first, leaving an ordinary inbox name.
        ("%2F%2Fevil.example", "evil.example"),
        # Merging leaves three segments (`https:`, host, date), so no route matches.
        ("https:%2F%2Fevil.example", None),
    ],
)
def test_hyphen_redirect_cannot_leave_the_site(client, hostile, encoded, thread):
    response = client.get(f"/{hostile}/2026-05-1" + ("/t" if thread else ""))
    if encoded is None:
        assert response.status_code == 404
        assert "Location" not in response.headers
        return
    assert response.status_code == 301
    location = response.headers["Location"]
    target = urlsplit(location)
    assert (target.scheme, target.netloc) == ("", "")
    assert location.startswith("/")
    assert not location.startswith(("//", "/\\"))
    assert location == f"/{encoded}/2026/05/1" + ("/t" if thread else "")


@pytest.mark.parametrize("prefix", ["/r/", "/all/"])
def test_lore_alias_uses_shared_canonical_inbox_policy(client, prefix):
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, Inbox

    for canonical in (None, "beta"):
        with SessionLocal() as session:
            article = session.scalar(
                select(Article).where(Article.message_id == "art3@example.com")
            )
            article.canonical_inbox_id = (
                session.scalar(select(Inbox.id).where(Inbox.name == canonical))
                if canonical
                else None
            )
            session.commit()
        expected = client.get("/m/art3@example.com")
        response = client.get(
            prefix + "%3Cart3@example.com%3E" + ("/" if prefix == "/all/" else "")
        )
        assert response.status_code == expected.status_code == 301
        assert response.headers["Location"] == expected.headers["Location"]
        assert response.headers["Cache-Control"] == expected.headers["Cache-Control"]
        assert "Content-Security-Policy" in response.headers
        assert "ETag" not in response.headers


@pytest.mark.parametrize(
    "path", ["/r/no-such@invalid.example", "/all/no-such@invalid.example/"]
)
def test_unknown_lore_alias_retains_lookup_help(client, path):
    from tests._dom import parse

    response = client.get(path)
    assert response.status_code == 404
    field = parse(response.get_data(as_text=True)).find("input", name="id")
    assert field.attrs["value"] == "no-such@invalid.example"
    assert "Cache-Control" not in response.headers


@pytest.mark.parametrize("prefix", ["/r/", "/all/"])
def test_lore_alias_rejects_controls_and_escapes_unknown_prefill(client, prefix):
    from tests._dom import parse

    suffix = "/" if prefix == "/all/" else ""
    assert client.get(prefix + "bad%0Aid@x" + suffix).status_code == 404
    response = client.get(prefix + "%3Cscript%3E@invalid.example" + suffix)
    assert response.status_code == 404
    page = parse(response.get_data(as_text=True))
    assert page.find("input", name="id").attrs["value"] == "<script>@invalid.example"
    assert "&lt;script&gt;@invalid.example" in response.get_data(as_text=True)


@pytest.mark.parametrize("thread", [False, True])
def test_hyphen_redirect_does_not_query_or_read_bodies(client, monkeypatch, thread):
    from sqlalchemy import event

    from mimir.extensions import engine

    statements = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    def no_body(*args, **kwargs):
        pytest.fail("redirect must not read message bodies")

    monkeypatch.setattr("mimir.store._blob_bytes", no_body)
    event.listen(engine, "before_cursor_execute", grab)
    try:
        response = client.get(
            "/unknown/2026-01-999999" + ("/t" if thread else "") + "?unused=value"
        )
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    assert response.status_code == 301
    assert statements == []
    assert "?" not in response.headers["Location"]
    assert "Content-Security-Policy" in response.headers


@pytest.mark.parametrize("thread", [False, True])
def test_hyphen_redirect_leaves_date_and_inbox_rules_to_acceptor(
    client, alternate_article, thread
):
    article_id, path = alternate_article
    _, year, month, _ = path.strip("/").split("/")
    suffix = "/t" if thread else ""
    wrong_month = 1 if int(month) != 1 else 2
    response = client.get(
        f"/alpha/{year}-{wrong_month}-{article_id}" + suffix, follow_redirects=True
    )
    assert response.status_code == 404
    assert response.history[0].status_code == 301
    # The seed inbox beta exists, but this article belongs to alpha only.
    response = client.get(
        f"/beta/{year}-{month}-{article_id}" + suffix, follow_redirects=True
    )
    if thread:
        assert response.status_code == 404
        assert response.request.path.startswith("/beta/")
    else:
        assert response.status_code == 200
        assert response.request.path.startswith("/alpha/")
        assert "alternate redirect target" in response.get_data(as_text=True)


@pytest.mark.parametrize("thread", [False, True])
def test_linked_crosspost_alias_preserves_the_slash_acceptor(
    client, tmp_path, alternate_article, thread
):
    from tests._dom import parse

    _, path = alternate_article
    beta_mirror = tmp_path / "beta"
    beta_mirror.mkdir()
    article_id, beta_path = _ingest_one_article(
        beta_mirror,
        "beta",
        "alternate/lookup@example.com",
        subject="alternate redirect target",
    )
    _, year, month, _ = path.strip("/").split("/")
    suffix = "/t" if thread else ""
    expected = client.get(beta_path + suffix, follow_redirects=True)
    assert expected.status_code == 200
    response = client.get(
        f"/beta/{year}-{month}-{article_id}" + suffix, follow_redirects=True
    )
    assert response.status_code == 200
    assert response.request.path == expected.request.path
    assert "alternate redirect target" in response.get_data(as_text=True)
    assert (
        parse(response.get_data(as_text=True))
        .find("link", rel="canonical")
        .attrs["href"]
        == parse(expected.get_data(as_text=True))
        .find("link", rel="canonical")
        .attrs["href"]
    )


def test_hyphen_rules_do_not_shadow_since_route(client):
    adapter = client.application.url_map.bind("localhost")
    endpoint, values = adapter.match("/alpha/since/2026-11-01")
    assert endpoint == "web.threads_since_view"
    assert values["since_str"] == "2026-11-01"


def test_all_alias_preserves_terminal_slash_in_message_id(client, tmp_path):
    _, target = _ingest_one_article(
        tmp_path, "alpha", "terminal@example.com/", subject="terminal slash target"
    )
    assert client.get(target).status_code == 200
    existing = client.get("/m/terminal@example.com/")
    assert existing.status_code == 301
    assert existing.headers["Location"] == target
    raw_r = client.get("/r/terminal@example.com/")
    assert raw_r.status_code == 301
    assert raw_r.headers["Location"] == target
    alias = client.get("/all/terminal@example.com//", follow_redirects=True)
    assert alias.history[0].status_code == 301
    assert alias.status_code == 200
    assert alias.request.path == target


def test_all_alias_does_not_resolve_another_message_id(client, tmp_path):
    terminal_dir = tmp_path / "terminal"
    ordinary_dir = tmp_path / "ordinary"
    terminal_dir.mkdir()
    ordinary_dir.mkdir()
    _, target = _ingest_one_article(
        terminal_dir, "alpha", "terminal@example.com/", subject="terminal slash target"
    )
    _, other = _ingest_one_article(
        ordinary_dir,
        "beta",
        "terminal@example.com",
        subject="different ordinary target",
    )
    assert target != other
    assert client.get(target).status_code == client.get(other).status_code == 200
    assert client.get("/m/terminal@example.com/").headers["Location"] == target
    assert client.get("/m/terminal@example.com").headers["Location"] == other
    alias = client.get("/all/terminal@example.com//", follow_redirects=True)
    assert alias.history[0].status_code == 301
    assert alias.status_code == 200
    assert alias.request.path == target
    assert "terminal slash target" in alias.get_data(as_text=True)
