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
