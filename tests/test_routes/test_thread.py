"""Tests for mimir/web/routes/thread.py: the whole-thread view, plus
the message page's canonical consolidation onto it."""

import re

from tests.test_routes._helpers import (
    _ingest_one_article,
    _json_ld_blocks,
    _seed_three_message_thread,
)


def _canonical_of(html: str) -> str | None:
    m = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    return m.group(1) if m else None


def test_thread_view_renders_every_message_under_one_h1(client, tmp_path):
    """The whole conversation on one page: each message's body inline,
    a single page-level <h1> (the thread subject) with per-message
    headings below it as <h2>, per the lead-with-one-h1 rule."""
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]

    html = client.get(root_url + "/t").get_data(as_text=True)
    assert html.count("<h1") == 1
    assert html.count('class="thread-message"') == 3
    # Every message's own page is still linked from the thread view, so
    # the deep links stay reachable and crawlable.
    for role in ("root", "reply", "nested"):
        _, url, _ = seeded[role]
        assert url in html


def test_thread_view_on_a_reply_redirects_to_the_root(client, tmp_path):
    """One thread, one URL. `/t` on a reply is a coherent request
    ("show me this conversation"), so it 301s to the root's thread view
    rather than 404ing or rendering a partial thread from the middle."""
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]
    _, reply_url, _ = seeded["reply"]

    r = client.get(reply_url + "/t")
    assert r.status_code == 301
    assert r.headers["Location"].endswith(root_url + "/t")


def test_thread_view_json_ld_carries_replies_as_comments(client, tmp_path):
    """The forum-thread shape: one DiscussionForumPosting for the root
    with every reply as a `comment`. This is the rich-result signal a
    single message page structurally cannot emit, and the reason the
    thread view is the indexed surface.

    `interactionStatistic` here counts the WHOLE thread, unlike the
    message page (where the entity is one message and the thread total
    would be a false claim)."""
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]

    blocks = _json_ld_blocks(client.get(root_url + "/t").data.decode())
    posting = next(
        g for g in blocks[0]["@graph"] if g["@type"] == "DiscussionForumPosting"
    )
    assert posting["@id"].endswith(root_url + "/t")
    comments = posting["comment"]
    assert len(comments) == 2
    assert all(c["@type"] == "Comment" for c in comments)
    assert posting["interactionStatistic"]["userInteractionCount"] == 2


def test_message_pages_canonicalise_to_the_thread_view(client, tmp_path):
    """Every message in the thread points its canonical at the
    conversation, so N thin near-duplicate URLs consolidate into one
    substantial document. The JSON-LD entity keeps the message's own
    `@id`: that entity IS this message, whatever the page canonical
    says."""
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]
    _, reply_url, _ = seeded["reply"]

    for url in (root_url, reply_url):
        html = client.get(url).get_data(as_text=True)
        assert _canonical_of(html).endswith(root_url + "/t"), url

    posting = next(
        g
        for g in _json_ld_blocks(client.get(reply_url).data.decode())[0]["@graph"]
        if g["@type"] == "DiscussionForumPosting"
    )
    assert posting["@id"].endswith(reply_url)


def test_single_message_thread_still_canonicalises_to_its_thread_view(
    client,
    tmp_path,
):
    """A one-message thread has a thread view too (it is its own root),
    so the canonical is uniform across the archive rather than
    conditional on thread length."""
    _, url = _ingest_one_article(tmp_path, "alpha", "solo@example.com")
    html = client.get(url).get_data(as_text=True)
    assert _canonical_of(html).endswith(url + "/t")
    assert client.get(url + "/t").status_code == 200


def test_thread_view_survives_a_message_whose_blob_is_missing(client, tmp_path):
    """A mirror gap on ONE message must not 404 the whole conversation:
    that message loses its body and keeps its header row, everything
    else renders. Simulated by pointing the article at a commit_sha
    that isn't in the mirror."""
    from sqlalchemy import select, update

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList

    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]
    reply_id, _, _ = seeded["reply"]

    with SessionLocal() as s:
        s.execute(
            update(ArticleList)
            .where(ArticleList.article_id == reply_id)
            .values(commit_sha="de" * 20)
        )
        s.commit()
        assert (
            s.execute(
                select(ArticleList.commit_sha).where(ArticleList.article_id == reply_id)
            ).scalar_one()
            == "de" * 20
        )

    r = client.get(root_url + "/t")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Message body unavailable" in html
    # The other two still rendered.
    assert html.count('class="thread-message"') == 3


def test_messages_past_the_render_cap_keep_their_own_canonical(
    client,
    tmp_path,
    monkeypatch,
):
    """The consolidation is conditional on containment. Past
    `thread_view_render_cap` the thread view only LINKS to a message
    rather than rendering it, so a canonical pointing there would claim
    the page contains content it doesn't. Those messages stay
    self-canonical.

    Cap forced to 2 against a 3-message thread, so the third message is
    the overflow case.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)

    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]
    _, reply_url, _ = seeded["reply"]
    _, nested_url, _ = seeded["nested"]

    # Inside the cap: consolidates onto the thread view.
    assert _canonical_of(client.get(root_url).get_data(as_text=True)).endswith(
        root_url + "/t"
    )
    assert _canonical_of(client.get(reply_url).get_data(as_text=True)).endswith(
        root_url + "/t"
    )
    # Past the cap: keeps its own URL, because the thread view does not
    # contain it.
    assert _canonical_of(client.get(nested_url).get_data(as_text=True)).endswith(
        nested_url
    )

    # ...and the thread view links to it instead of inlining it.
    html = client.get(root_url + "/t").get_data(as_text=True)
    assert html.count('class="thread-message"') == 2
    assert "further message" in html
    assert nested_url in html
