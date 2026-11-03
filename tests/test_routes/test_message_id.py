"""Tests for mimir/web/routes/message_id.py: the `/m/<id>`
unscoped redirect and the `/<inbox>/m/<id>` scoped form,
including 301 vs 302 contract and canonical-inbox routing."""

import pytest

from tests.test_routes._helpers import _any_article_in


@pytest.mark.parametrize("prefix", ["/m", "/{inbox}/m"])
def test_message_id_lookup_301_redirects_to_canonical(client, inbox_name, prefix):
    """Both the global Message-ID lookup (`/m/<id>`) and the
    inbox-scoped form (`/<inbox>/m/<id>`) 301 to the article's
    canonical URL. Parametrized over the URL prefix so the redirect
    contract is pinned exactly once."""
    art = _any_article_in(inbox_name)
    if art is None:
        pytest.skip("no articles in DB")
    url = prefix.format(inbox=inbox_name) + f"/{art.message_id}"
    r = client.get(url, follow_redirects=False)
    assert r.status_code == 301
    loc = r.headers.get("Location", "")
    assert loc.endswith(f"/{art.id}")
    # Inbox-scoped form must resolve to the same inbox's URL space;
    # the global form may go to any canonical inbox so we only pin
    # the trailing id.
    if prefix.startswith("/{inbox}"):
        assert loc.startswith(f"/{inbox_name}/")
    # Cache header on the redirect itself -- crawlers should be able
    # to cache the hop.
    assert r.headers.get("Cache-Control", "").startswith("public")


def test_message_id_404_unknown(client):
    assert client.get("/m/no-such-message-id-12345").status_code == 404


def test_global_message_id_lookup_uses_canonical_inbox(client):
    """art3 is cross-posted alpha+beta. Setting canonical_inbox_id to
    beta makes /m/<id> redirect to /beta/.../<id>, not /alpha/.../<id>
    (which would be the alphabetical-first fallback)."""
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, Inbox

    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        art = s.execute(
            select(Article).where(Article.message_id == "art3@example.com")
        ).scalar_one()
        art.canonical_inbox_id = beta.id
        s.commit()

    r = client.get("/m/art3@example.com", follow_redirects=False)
    assert r.status_code == 301
    loc = r.headers.get("Location", "")
    assert "/beta/" in loc
    assert "/alpha/" not in loc


def test_global_message_id_lookup_falls_back_to_alphabetical(client):
    """With canonical_inbox_id NULL (default for seeded art3), the
    redirect goes to alphabetical-first, alpha."""
    r = client.get("/m/art3@example.com", follow_redirects=False)
    assert r.status_code == 301
    loc = r.headers.get("Location", "")
    assert "/alpha/" in loc


# Per-page meta description + Open Graph + Twitter Card tags


# The bare `/m/?id=` form route (#984): same lookup as `/m/<id>`, reached
# by a GET form, so the input arrives in a query string.


def test_form_route_redirects_like_the_path_route(client):
    by_path = client.get("/m/art3@example.com", follow_redirects=False)
    by_form = client.get("/m/?id=art3@example.com", follow_redirects=False)
    assert by_path.status_code == by_form.status_code == 301
    assert by_form.headers["Location"] == by_path.headers["Location"]
    assert by_form.headers["Cache-Control"].startswith("public")


@pytest.mark.parametrize(
    "typed",
    [
        "<art3@example.com>",
        "mid:art3@example.com",
        "https://lore.kernel.org/linux-fsdevel/art3@example.com/",
        "/all/art3@example.com/",
        "/r/art3@example.com",
        "/alpha/art3@example.com/",
    ],
)
def test_form_route_accepts_every_shape_the_normaliser_does(client, typed):
    expected = client.get("/m/art3@example.com", follow_redirects=False)
    r = client.get("/m/", query_string={"id": typed}, follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["Location"] == expected.headers["Location"]


@pytest.mark.parametrize("query", ["", "?id=", "?id=%20", "?id=no-such-id@x.org"])
def test_form_route_404s_on_empty_or_unknown(client, query):
    r = client.get("/m/" + query, follow_redirects=False)
    assert r.status_code == 404
    assert "Cache-Control" not in r.headers


def test_form_route_never_redirects_to_the_typed_text(client):
    """The Location comes from the database row, not from the input, so
    the form cannot be turned into an open redirect."""
    typed = "https://evil.example/x/art3@example.com/"
    r = client.get("/m/", query_string={"id": typed}, follow_redirects=False)
    assert r.status_code == 301
    assert "evil.example" not in r.headers["Location"]
    assert r.headers["Location"].startswith("/")


@pytest.mark.parametrize("prefix", ["/m/", "/alpha/m/"])
def test_path_routes_accept_a_bracketed_id(client, prefix):
    """The path routes read through the normaliser too (a documented
    widening: `%3C...%3E` used to 404)."""
    r = client.get(prefix + "%3Cart1@example.com%3E", follow_redirects=False)
    assert r.status_code == 301


def test_path_routes_still_404_on_junk(client):
    assert client.get("/m/%20", follow_redirects=False).status_code == 404
    assert client.get("/alpha/m/%20", follow_redirects=False).status_code == 404


def _render_form(client, **kwargs):
    env = client.application.jinja_env
    arg = ", ".join(f"{k}={k}" for k in kwargs)
    source = (
        '{% from "_message_id_form.html" import message_id_form %}'
        f"{{{{ message_id_form({arg}) }}}}"
    )
    with client.application.test_request_context("/"):
        return env.from_string(source).render(**kwargs)


def test_form_partial_is_a_get_form_to_the_lookup_route(client):
    from html.parser import HTMLParser

    html = _render_form(client)
    tags = []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            tags.append((tag, dict(attrs)))

    P().feed(html)
    form = next(a for t, a in tags if t == "form")
    field = next(a for t, a in tags if t == "input")
    assert form["action"] == "/m/"
    assert form["method"] == "get"
    assert field["name"] == "id"
    assert field["maxlength"] == "998"
    assert "style" not in form and "style" not in field
    # The label points at the field.
    label = next(a for t, a in tags if t == "label")
    assert label["for"] == field["id"]


def test_form_partial_escapes_the_prefill(client):
    html = _render_form(client, value='"><script>alert(1)</script>')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize(
    "typed",
    [
        "https://lore.kernel.org/all/art3@example.com/T/#u",
        "https://lore.kernel.org/linux-fsdevel/art3@example.com/T/",
        "https://lore.kernel.org/linux-fsdevel/art3@example.com/raw",
        "https://lore.kernel.org/linux-fsdevel/art3@example.com/t.mbox.gz",
    ],
)
def test_form_route_opens_lore_thread_and_raw_links(client, typed):
    from mimir.msgid_url import msgid_from_url

    assert msgid_from_url(typed) == "art3@example.com"  # the linkifier reads it
    good = client.get("/m/art3@example.com", follow_redirects=False)
    r = client.get("/m/", query_string={"id": typed}, follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["Location"] == good.headers["Location"]
