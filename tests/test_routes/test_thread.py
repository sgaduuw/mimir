"""Tests for mimir/web/routes/thread.py: the whole-thread view, plus
the message page's canonical consolidation onto it."""

import re

import pytest

from mimir.models import Article
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


def test_single_message_thread_does_not_canonicalise_to_its_thread_view(
    client,
    tmp_path,
):
    """A one-message thread has nothing to consolidate: the message
    page already IS the whole conversation, and it is the richer of the
    two surfaces (subsystem header, lifecycle badges, the indexable
    lifecycle prose, attachments). Handing its authority to `/t` would
    trade all of that away for no gain, so it keeps its own canonical.

    The `/t` page still exists and renders; it is simply not the
    canonical target."""
    _, url = _ingest_one_article(tmp_path, "alpha", "solo@example.com")
    html = client.get(url).get_data(as_text=True)
    assert _canonical_of(html).endswith(url)
    assert not _canonical_of(html).endswith("/t")
    assert client.get(url + "/t").status_code == 200


def test_thread_view_survives_a_message_whose_blob_is_missing(client, tmp_path):
    """A mirror gap on ONE message must not 404 the whole conversation:
    that message loses its body and keeps its header row, everything
    else renders. Simulated by pointing the article at a commit_sha
    that isn't in the mirror."""
    from sqlalchemy import update

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


def _cross_post(seeded, roles, *, canonical_for=None):
    """Link the named seeded roles into `beta` as well as `alpha`,
    reusing alpha's mirror and blob pointers so both inboxes resolve
    the same messages. Optionally pin one article's canonical inbox."""
    from sqlalchemy import select as sa_select

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList, Inbox

    with SessionLocal() as s:
        alpha = s.execute(sa_select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        other = s.execute(sa_select(Inbox).where(Inbox.name == "beta")).scalar_one()
        other.mirror_path = alpha.mirror_path
        for role in roles:
            aid, _, _ = seeded[role]
            link = s.execute(
                sa_select(ArticleList).where(
                    ArticleList.article_id == aid,
                    ArticleList.inbox_id == alpha.id,
                )
            ).scalar_one()
            s.add(
                ArticleList(
                    article_id=aid,
                    inbox_id=other.id,
                    epoch=link.epoch,
                    commit_sha=link.commit_sha,
                )
            )
        if canonical_for is not None:
            aid, _, _ = seeded[canonical_for]
            s.get(Article, aid).canonical_inbox_id = other.id
        s.commit()


def test_canonical_never_points_at_a_thread_without_the_message(client, tmp_path):
    """Threading is inbox-scoped, so a message's thread ROOT need not
    exist in the message's canonical inbox even though the message
    does. Building the thread-view canonical on the canonical inbox
    therefore produced a hard 404 (root absent there), or a thread that
    genuinely did not contain the message.

    Seeds exactly that: the reply is cross-posted to beta and pinned
    canonical there, while its root stays alpha-only.
    """
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]
    _, reply_url, _ = seeded["reply"]
    _cross_post(seeded, ["reply"], canonical_for="reply")

    canonical = _canonical_of(client.get(reply_url).get_data(as_text=True))
    target = canonical.replace("http://localhost", "")

    resolved = client.get(target)
    assert resolved.status_code == 200, f"canonical {target} does not resolve"
    # ...and it actually contains the message that points at it.
    assert reply_url in resolved.get_data(as_text=True)
    assert target == root_url + "/t"


def test_thread_views_are_self_canonical_per_inbox(client, tmp_path):
    """Cross-inbox thread consolidation was tried and reverted.

    `get_thread` is inbox-scoped, so the "same" thread has different
    membership per inbox: a reply that trimmed one list from its Cc is
    absent there. Pointing one inbox's thread page at another's hands
    authority to a page that may omit content this one renders, and the
    target may not even treat the same article as its root (so the
    canonical can land on a 301). Root-membership gating does not help,
    because root membership says nothing about reply membership.

    So each inbox's thread view is self-canonical. The residual
    duplication is small (message-level consolidation already collapsed
    N messages to one page per inbox) and search engines dedupe
    near-identical pages themselves. A truthful weaker signal beats a
    stronger false one.
    """
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]
    _cross_post(seeded, ["root", "reply", "nested"])

    beta_url = root_url.replace("/alpha/", "/beta/") + "/t"
    assert _canonical_of(client.get(root_url + "/t").get_data(as_text=True)).endswith(
        root_url + "/t"
    )
    assert _canonical_of(client.get(beta_url).get_data(as_text=True)).endswith(beta_url)


def test_thread_view_survives_a_render_cap_below_one(client, tmp_path, monkeypatch):
    """`THREAD_VIEW_RENDER_CAP=0` rendered no messages and then hit an
    IndexError building JSON-LD (which needs a root), 500ing every
    thread view. An unusable ops value must degrade, not take the
    surface down."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 0)
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]

    r = client.get(root_url + "/t")
    assert r.status_code == 200
    assert r.get_data(as_text=True).count('class="thread-message"') == 1


@pytest.mark.parametrize(
    ("roles", "pin"),
    [
        pytest.param([], None, id="no-cross-post"),
        pytest.param(["root"], "root", id="root-only-pinned"),
        pytest.param(["reply"], "reply", id="reply-only-pinned"),
        pytest.param(["root", "reply"], "reply", id="root+reply-pinned-reply"),
        pytest.param(["root", "reply", "nested"], "root", id="all-pinned-root"),
        pytest.param(["root", "reply", "nested"], None, id="all-unpinned"),
    ],
)
def test_every_message_canonical_resolves_and_contains_that_message(
    client,
    tmp_path,
    roles,
    pin,
):
    """The invariant, checked across every cross-post shape rather than
    one hand-picked case.

    A canonical must (a) resolve to a 200, not a 404 and not a
    redirect, and (b) point at a page that actually contains the
    message pointing at it. Two successive rounds of blocking bugs both
    violated exactly this, each time in a cross-post shape the
    then-current tests did not seed: first because the thread root was
    resolved in one inbox and the URL built in another, then because a
    thread page consolidated onto another inbox whose copy of the
    thread had different membership.

    Enumerating the shapes is what makes this a guard rather than
    another hand-picked case that happens to pass. Parametrised rather
    than looped so each shape gets a freshly reset DB.
    """
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    if roles:
        _cross_post(seeded, roles, canonical_for=pin)

    for role in ("root", "reply", "nested"):
        _, url, _ = seeded[role]
        page = client.get(url)
        assert page.status_code == 200, f"{url} itself is {page.status_code}"
        canonical = _canonical_of(page.get_data(as_text=True))
        assert canonical, f"{url} emitted no canonical"

        target = canonical.replace("http://localhost", "")
        resolved = client.get(target)
        assert resolved.status_code == 200, (
            f"{url} canonicalises to {target} which returns {resolved.status_code}"
        )
        # Either the canonical IS this page, or it is a page that
        # renders this message inline (its permalink appears in the
        # rendered conversation).
        assert target == url or url in resolved.get_data(as_text=True), (
            f"{url} canonicalises to {target}, which does not contain it"
        )


def test_thread_view_carries_the_roots_patch_surfaces(client, tmp_path):
    """Message pages in a multi-message thread canonicalise here, so
    the target must not be the poorer document.

    Without this the consolidation would drop exactly the lifecycle
    prose this release added as indexable text, plus the subsystem
    attribution, i.e. the archive's distinctive content would stop
    being on the page search engines actually index.
    """

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleFile, MainlineCommit
    from tests.test_routes._helpers import _seed_subsystem

    _seed_subsystem("BCACHEFS", "Supported", ["fs/bcachefs/"])
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    root_id, root_url, root_mid = seeded["root"]

    with SessionLocal() as s:
        # Make the root look like a landed patch touching a known
        # subsystem: file rows drive the subsystem match, a linus
        # mainline commit drives the LANDED lifecycle + prose.
        # `patch_state.is_patch` keys off a `[PATCH ...]`-shaped
        # subject, which the generic thread fixture doesn't have.
        s.get(Article, root_id).subject = "[PATCH] bcachefs: fix a thing"
        s.add(ArticleFile(article_id=root_id, path="fs/bcachefs/btree.c"))
        s.add(
            MainlineCommit(
                commit_sha="f" * 40,
                message_id=root_mid,
                tree_name="linus",
                committed_at=s.get(Article, root_id).date,
            )
        )
        s.commit()

    html = client.get(root_url + "/t").get_data(as_text=True)
    assert "bcachefs" in html.lower(), "subsystem attribution missing"
    assert "landed in mainline as ffffffffffff" in html.lower(), (
        "lifecycle synthesis prose missing from the canonical target"
    )
