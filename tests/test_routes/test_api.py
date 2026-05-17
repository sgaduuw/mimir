"""Tests for mimir/web/routes/api.py: the HTMX `recent`
load-more endpoint and any other lightweight JSON / partial
API surfaces."""


def test_api_recent_returns_partial_with_article_links(client, inbox_name):
    """The HTMX load-more endpoint returns the `_recent_items.html`
    fragment -- bare <li>s, no <html>/<head> wrapping -- with <a>
    links pointing at messages in this inbox. A response that
    somehow included the full layout would still be `200 OK` but
    would break htmx-target swaps."""
    r = client.get(f"/api/{inbox_name}/recent?offset=0")
    assert r.status_code == 200
    body = r.data.decode()
    # Fragment, not a full page.
    lower = body.lower()
    assert "<html" not in lower
    assert "<head>" not in lower and "<head " not in lower
    assert "<body" not in lower
    # At least one article-link <li>. The exact count depends on
    # seeded data (alpha has 3, fewer than the page size), so we
    # bound the lower side and check the structural shape.
    li_count = body.count("<li")
    assert li_count >= 1, f"no <li> in partial: {body!r}"
    assert f'href="/{inbox_name}/' in body, (
        f"partial must link to /{inbox_name}/<...>: {body!r}"
    )


def test_api_recent_unknown_inbox_returns_404(client):
    """Same 404 contract as the rest of the inbox-scoped routes."""
    assert client.get("/api/no-such-inbox/recent").status_code == 404
