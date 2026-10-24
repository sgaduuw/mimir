"""3.27.0 release review: #845/#850 name a nameless allowlisted sender by
its address in JSON-LD and Atom; the message page's meta description
still says "unknown sender" for the same sender.
"""

from mimir.config import settings
from tests.test_routes._helpers import (
    _ingest_one_article,
    _json_ld_blocks,
    _meta_value,
)


def test_meta_description_names_sender_as_json_ld_does(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "email_allowlist", ["@open.example"])
    _, url = _ingest_one_article(
        tmp_path, "alpha", "meta-name@x", subject="hello", author="ok@open.example"
    )
    html = client.get(url).get_data(as_text=True)
    graph = _json_ld_blocks(html)[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    assert posting["author"]["name"] == "ok@open.example"  # precondition
    description = _meta_value(html, "description")
    assert "unknown sender" not in description
    assert "ok@open.example" in description
