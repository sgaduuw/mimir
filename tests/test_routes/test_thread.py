"""Tests for mimir/web/routes/thread.py: the whole-thread view, plus
the message page's canonical consolidation onto it."""

import re

import pytest

from mimir.models import Article
from tests.test_routes._helpers import (
    _ingest_one_article,
    _json_ld_blocks,
    _seed_three_message_thread,
    seed_thread_shape,
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


def test_thread_view_json_ld_nests_replies_under_their_parent(client, tmp_path):
    """The forum-thread shape, with the reply structure intact.

    One `DiscussionForumPosting` for the root, and each reply attached
    to the message it actually answers. Until 3.8.0 every reply was
    attached directly to the root, which asserts to crawlers that a
    reply-to-a-reply answers the original post: a false structural
    claim about the one document every message in the thread
    canonicalises to.

    The fixture is a three-deep CHAIN on purpose. A two-level fixture
    passes the flat implementation trivially, which is how the
    flattening shipped, so the assertion that matters is the negative
    one: the grandchild must NOT be a top-level comment.

    `interactionStatistic` still counts the WHOLE thread, unlike the
    message page (where the entity is one message and the thread total
    would be a false claim).
    """
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]
    reply_id, reply_url, _ = seeded["reply"]
    nested_id, nested_url, _ = seeded["nested"]

    blocks = _json_ld_blocks(client.get(root_url + "/t").data.decode())
    posting = next(
        g for g in blocks[0]["@graph"] if g["@type"] == "DiscussionForumPosting"
    )
    assert posting["@id"].endswith(root_url + "/t")
    assert posting["interactionStatistic"]["userInteractionCount"] == 2

    top = posting["comment"]
    assert len(top) == 1, (
        f"the grandchild was attached to the root; top-level comments: "
        f"{[c['@id'] for c in top]}"
    )
    assert top[0]["@type"] == "Comment"
    assert top[0]["@id"].endswith(reply_url)

    nested = top[0].get("comment")
    assert nested and len(nested) == 1, (
        "the reply's own reply is missing from the structure entirely"
    )
    assert nested[0]["@id"].endswith(nested_url)

    # The negative, stated separately because it is the whole point.
    assert not any(c["@id"].endswith(nested_url) for c in top), (
        "a reply-to-a-reply is still claimed as a reply to the root"
    )


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


def test_messages_past_the_cap_canonicalise_to_the_page_holding_them(
    client,
    tmp_path,
    monkeypatch,
):
    """The linear case, and that the overflow list is gone.

    Split from the bushy test above because `seed_thread_shape` and
    `_seed_three_message_thread` both write `0.git` into `tmp_path`.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)

    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]
    _, reply_url, _ = seeded["reply"]
    _, nested_url, _ = seeded["nested"]

    # Page 1 holds the first two.
    assert _canonical_of(client.get(root_url).get_data(as_text=True)).endswith(
        root_url + "/t"
    )
    assert _canonical_of(client.get(reply_url).get_data(as_text=True)).endswith(
        root_url + "/t"
    )
    # The third is on page 2, and says so.
    assert _canonical_of(client.get(nested_url).get_data(as_text=True)).endswith(
        root_url + "/t/2"
    )

    # And that page really does contain it: a canonical naming a page
    # that does not hold the message is the failure this asserts away.
    page1 = client.get(root_url + "/t").get_data(as_text=True)
    page2 = client.get(root_url + "/t/2").get_data(as_text=True)
    assert page1.count('class="thread-message"') == 2
    assert page2.count('class="thread-message"') == 1
    nested_id = int(nested_url.rsplit("/", 1)[1])
    assert f'id="m{nested_id}"' in page2, (
        "message canonicalises to page 2 but page 2 does not render it"
    )
    # The overflow list it replaced is gone.
    assert "further message" not in page1


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
    """Round-1 regression, kept because it names the specific bug.

    Threading is inbox-scoped, so a message's thread ROOT need not
    exist in the message's canonical inbox even though the message
    does. Building the thread-view canonical from the requested
    inbox's root while naming the canonical inbox produced a hard 404.

    Seeds exactly that: the reply is cross-posted to beta and pinned
    canonical there, while its root stays alpha-only. Consolidation now
    resolves against BETA's copy of the conversation, where the reply
    is a singleton, so the canonical is beta's message page rather than
    any thread view. What matters is the invariant, not which URL wins:
    it resolves, contains the message, and is terminal.
    """
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, reply_url, _ = seeded["reply"]
    _cross_post(seeded, ["reply"], canonical_for="reply")

    _assert_canonical_invariants(
        client,
        [reply_url, reply_url.replace("/alpha/", "/beta/")],
        "root-not-in-target",
    )
    canonical = _canonical_of(client.get(reply_url).get_data(as_text=True))
    assert canonical.endswith(reply_url.replace("/alpha/", "/beta/"))


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
        # Unpinned: canonical falls back to the alphabetically-first
        # inbox, so the OTHER arm's canonical points across inboxes.
        # This is the shape that produces a chain.
        pytest.param(["reply"], None, id="reply-only-unpinned"),
        pytest.param(["root", "reply"], None, id="root+reply-unpinned"),
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

    urls = []
    for role in ("root", "reply", "nested"):
        _, url, _ = seeded[role]
        urls.append(url)
        # A cross-posted article is reachable under EVERY linked inbox,
        # and each of those URLs has to satisfy the contract too. Only
        # checking the origin inbox is how the previous round missed a
        # canonical chain that only appears from the other arm.
        if role in roles:
            urls.append(url.replace("/alpha/", "/beta/"))
    _assert_canonical_invariants(client, urls, f"roles={roles} pin={pin}")


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


def _assert_canonical_invariants(client, urls, label):
    """The full canonical contract, asserted for every URL in `urls`.

    Stricter than the earlier six-shape guard, which varied only the
    cross-post dimension over a single tidy 3-message linear thread and
    so could not see thread-graph pathologies at all. Checks four
    things, because previous rounds each satisfied some subset:

    1. the canonical RESOLVES: 200, not 404 and not a redirect;
    2. it CONTAINS the message (or is the page itself);
    3. it is TERMINAL, i.e. the target does not itself canonicalise
       somewhere else. A chain is a defect even when every hop is
       individually truthful, which is the shape that slipped through
       the last round;
    4. any `/t` URL reached terminates rather than looping.
    """
    for url in urls:
        page = client.get(url)
        assert page.status_code == 200, f"{label}: {url} -> {page.status_code}"
        canonical = _canonical_of(page.get_data(as_text=True))
        assert canonical, f"{label}: {url} emitted no canonical"
        target = canonical.replace("http://localhost", "")

        resolved = client.get(target)
        assert resolved.status_code == 200, (
            f"{label}: {url} canonicalises to {target} -> {resolved.status_code}"
        )
        body = resolved.get_data(as_text=True)
        # Containment is by ARTICLE, not by URL string: a cross-inbox
        # canonical renders the same article under the target inbox's
        # path, so matching the requesting URL verbatim would report a
        # false violation. The trailing `"` anchors the id so article 5
        # doesn't match article 15.
        article_id = url.rsplit("/", 1)[-1]
        contained = re.search(rf'/\d{{4}}/\d{{2}}/{article_id}"', body) is not None
        assert target == url or contained, (
            f"{label}: {url} canonicalises to {target}, which does not contain it"
        )
        onward = _canonical_of(body)
        if onward:
            assert onward.replace("http://localhost", "") == target, (
                f"{label}: canonical chain {url} -> {target} -> "
                f"{onward.replace('http://localhost', '')}"
            )


def _assert_no_redirect_loop(client, urls, label, limit=6):
    """Following `/t` must terminate. A `thread_parent` cycle is
    constructible by anyone who can post to the list (they control both
    their Message-ID and their In-Reply-To), and `find_thread_root`
    walks to MAX_DEPTH rather than to a fixed point, so the redirect
    target need not be its own root."""
    for url in urls:
        seen = []
        cur = url + "/t"
        for _ in range(limit):
            r = client.get(cur)
            if r.status_code != 301:
                break
            assert cur not in seen, f"{label}: /t redirect loop at {cur} ({seen})"
            seen.append(cur)
            cur = r.headers["Location"].replace("http://localhost", "")
        else:
            raise AssertionError(
                f"{label}: /t did not settle within {limit} hops: {seen}"
            )


THREAD_SHAPES = {
    "linear-3": [("s1@x", None), ("s2@x", "s1@x"), ("s3@x", "s2@x")],
    "branched": [
        ("b1@x", None),
        ("b2@x", "b1@x"),
        ("b3@x", "b1@x"),
        ("b4@x", "b2@x"),
    ],
    "deep-6": [(f"d{i}@x", None if i == 1 else f"d{i - 1}@x") for i in range(1, 7)],
    "single": [("solo1@x", None)],
    "off-list-parent": [("o1@x", "missing@elsewhere"), ("o2@x", "o1@x")],
    # Pathological, and reachable: `thread_parent` has no cycle guard
    # and its value comes straight from sender-controlled headers.
    "self-parent": [("c0@x", "c0@x")],
    "cycle-3": [("y1@x", "y3@x"), ("y2@x", "y1@x"), ("y3@x", "y2@x")],
}


@pytest.mark.parametrize("shape", sorted(THREAD_SHAPES))
def test_canonical_invariants_hold_across_thread_shapes(client, tmp_path, shape):
    """The guard, re-armed along the axis it was previously blind to.

    The prior version parametrised six CROSS-POST shapes over exactly
    one THREAD shape (3 messages, linear, acyclic, all dated, same
    month). An adversarial pass broke three of four novel thread shapes
    using the guard's own assertion body, so the coverage claim was
    false even though every parameter passed.
    """
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", THREAD_SHAPES[shape])
    urls = [url for _id, url in seeded.values()]
    _assert_no_redirect_loop(client, urls, shape)
    _assert_canonical_invariants(client, urls, shape)


def test_sitemap_lists_the_canonical_url_for_every_thread_shape(client, tmp_path):
    """Whatever the sitemap lists must be the page that page's own
    canonical points at.

    For a single-message thread the message page is canonical (it IS
    the whole conversation, and carries the patch surfaces `/t` omits),
    so listing `/t` instead publishes the thinner of two self-canonical
    near-duplicates and drops the canonical one entirely, with nothing
    relating them. `message.py` already carries this rule; the sitemap
    has to mirror it.

    Scoped to a SINGLE inbox on purpose. Across inboxes the rule is
    deliberately weaker: a cross-posted article is listed in every
    linked inbox's sitemap even though its canonical elects one of
    them, which is the pre-existing accepted behaviour (each entry is
    a real crawlable URL and the page's own canonical resolves which
    to keep). Asserting the strong form globally would fail on that
    by design.
    """
    import xml.etree.ElementTree as ET

    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [("solo-sm@x", None), ("m1-sm@x", None), ("m2-sm@x", "m1-sm@x")],
    )
    from mimir import cache

    cache.delete_for_inbox("alpha")
    cache.delete("sitemap:index")

    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(client.get("/alpha/sitemap.xml").get_data())
    locs = {u.find("s:loc", ns).text.replace("http://localhost", "") for u in root}

    for mid, (_aid, url) in seeded.items():
        listed = {loc for loc in locs if loc in (url, url + "/t")}
        if not listed:
            continue
        canonical = _canonical_of(client.get(url).get_data(as_text=True)).replace(
            "http://localhost", ""
        )
        assert listed == {canonical}, (
            f"{mid}: sitemap lists {listed} but the canonical is {canonical}"
        )


def test_thread_view_never_renders_the_same_message_twice(client, tmp_path):
    """A self-referential `In-Reply-To` (sender-controlled, and the
    likeliest ACCIDENTAL cycle) makes `get_thread`'s recursive CTE
    re-emit one article once per level to MAX_DEPTH, so the page
    rendered the same body dozens of times and emitted hundreds of
    identical overflow links from a single message."""
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("dup1@x", "dup1@x")])
    _aid, url = seeded["dup1@x"]

    html = client.get(url + "/t").get_data(as_text=True)
    assert html.count('class="thread-message"') == 1, "duplicate nodes rendered"
    assert "further message" not in html, "duplicate nodes leaked into overflow"


@pytest.mark.parametrize("surface", ["message", "thread"])
def test_etag_is_stable_across_identical_requests_and_304s(client, tmp_path, surface):
    """The other direction, and the one every other guard here is blind
    to: an unchanged page must keep its validator.

    Both routes are `public, no-cache`, so a validator that differs per
    request defeats conditional GET completely: the CDN and every
    crawler re-fetch a full body on every revalidation, on the surface
    the sitemap points them at. Every other ETag test asserts only that
    the tag MOVED, which a per-request-random tag satisfies by
    construction, so nothing would notice.

    Asserts the round trip too, not just tag equality: an
    `If-None-Match` carrying the tag must actually produce a 304.
    """
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("stab@x", None), ("stab2@x", "stab@x")]
    )
    _root_id, root_url = seeded["stab@x"]
    url = root_url if surface == "message" else root_url + "/t"

    tags = {client.get(url).headers["ETag"] for _ in range(4)}
    assert len(tags) == 1, f"{surface} ETag is not stable across requests: {tags}"

    etag = tags.pop()
    conditional = client.get(url, headers={"If-None-Match": etag})
    assert conditional.status_code == 304, (
        f"{surface} did not honour its own ETag on revalidation"
    )
    assert conditional.get_data() == b""


def test_thread_view_opens_the_epoch_repo_once_for_the_whole_render(
    client, tmp_path, monkeypatch
):
    """Pins the fix at the ROUTE, not just in `store.read_messages`.

    The helper's own tests pass just as well against the per-message
    shape this replaced, because they call the helper directly. Only a
    render can show that the route actually uses it, and the saving is
    entirely in the number of pack-index reopens.
    """
    from tests.test_routes._helpers import count_repo_opens, seed_thread_shape

    ids = [f"open{i}@x" for i in range(5)]
    seeded = seed_thread_shape(
        tmp_path, "alpha", [(ids[0], None)] + [(m, ids[0]) for m in ids[1:]]
    )
    _root_id, root_url = seeded[ids[0]]

    opened = count_repo_opens(monkeypatch)

    html = client.get(root_url + "/t").get_data(as_text=True)
    assert html.count('class="thread-message"') == 5, "not all messages rendered"
    assert len(opened) == 1, (
        f"one repo open for a single-epoch thread, got {len(opened)}: {opened}"
    )


def _prepare_rules_version(seeded):
    from mimir.extensions import SessionLocal
    from mimir.models import MainlineState

    with SessionLocal() as s:
        s.add(MainlineState(tree_name="linus", last_commit_sha="a" * 40))
        s.commit()


def _mutate_rules_version(seeded):
    """A MAINTAINERS reparse. It rewrites which subsystems claim the
    article and adds no message, so it moves neither node count nor
    thread max date. Versioned through `MainlineState.last_commit_sha`
    (the MAINTAINERS blob id at the last load) so one scalar covers the
    whole rule snapshot rather than re-deriving this article's matches
    before the 304."""
    from mimir.extensions import SessionLocal
    from mimir.models import MainlineState

    with SessionLocal() as s:
        s.get(MainlineState, "linus").last_commit_sha = "b" * 40
        s.commit()


def _mutate_mainline_landing(seeded):
    """The patch lands in Linus's tree. Flips the lifecycle badge to
    LANDED and rewrites the "did $series land" prose, while adding no
    message to the thread. Same class as the 3.6.1 sitemap incident: a
    validator that cannot see the change pins the wrong answer at the
    edge and in every crawler until a deploy bumps the version."""
    from datetime import datetime, timezone

    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.add(
            MainlineCommit(
                commit_sha="a" * 40,
                message_id="root@x",
                tree_name="linus",
                committed_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
            )
        )
        s.commit()


def _prepare_first_landing(seeded):
    from datetime import datetime, timezone

    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.add(
            MainlineCommit(
                commit_sha="c" * 40,
                message_id="root@x",
                tree_name="linux-next",
                committed_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
            )
        )
        s.commit()


def _mutate_second_landing(seeded):
    """The patch reaches a SECOND tree (aggregated into linux-next,
    then landed in Linus). Moves the landing COUNT while the existing
    row, and so the max date, could stay put.

    Split from `_mutate_mainline_landing` because that one inserts into
    an empty table, moving count 0->1 and max None->date at once: each
    half covers for the other, and dropping either from the tag leaves
    the guard green."""
    from datetime import datetime, timezone

    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.add(
            MainlineCommit(
                commit_sha="d" * 40,
                message_id="root@x",
                tree_name="linus",
                # Deliberately NOT newer, so only the count moves.
                committed_at=datetime(2024, 5, 1, tzinfo=timezone.utc),
            )
        )
        s.commit()


def _mutate_relanded_in_rebasing_tree(seeded):
    """A rebasing tree (`linux-next`, `rebases=True`) DELETEs its rows
    and re-walks daily, so the same message reappears under a new
    commit sha with a new committer date at UNCHANGED count. This is
    the case that moves only the max, and the only one that pins it."""
    from datetime import datetime, timezone

    from sqlalchemy import delete

    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.execute(delete(MainlineCommit).where(MainlineCommit.message_id == "root@x"))
        s.add(
            MainlineCommit(
                commit_sha="e" * 40,
                message_id="root@x",
                tree_name="linux-next",
                committed_at=datetime(2024, 9, 9, tzinfo=timezone.utc),
            )
        )
        s.commit()


def _mutate_review_trailer(seeded):
    """A `Reviewed-by:` arrives on a REPLY and is attributed to the
    root, moving the roll-up count in the lifecycle pill. Re-ingest of
    the root (or a trailer-parser fix backfilled) does the same without
    any new message."""
    from mimir.extensions import SessionLocal
    from mimir.models import ArticleTrailer

    root_id, _url = seeded["root@x"]
    with SessionLocal() as s:
        s.add(
            ArticleTrailer(
                article_id=root_id,
                role="Reviewed-by",
                name="R Eviewer",
                address="r@example.com",
                address_normalized="r@example.com",
            )
        )
        s.commit()


def _prepare_article_file(seeded):
    from mimir.extensions import SessionLocal
    from mimir.models import ArticleFile

    root_id, _url = seeded["root@x"]
    with SessionLocal() as s:
        s.add(ArticleFile(article_id=root_id, path="drivers/net/before.c"))
        s.commit()


def _mutate_article_file(seeded):
    """`backfill-article-files --reprocess`, or a re-ingest after a
    parser fix, rewrites a touched path IN PLACE. The subsystem line is
    rules x `article_files`, so this changes which subsystems claim the
    article while the row COUNT stays put. A `count(*)` validator is
    satisfied by construction on exactly this mutation, which is why
    the tag digests the paths themselves."""
    from sqlalchemy import delete

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleFile

    root_id, _url = seeded["root@x"]
    with SessionLocal() as s:
        s.execute(delete(ArticleFile).where(ArticleFile.article_id == root_id))
        s.add(ArticleFile(article_id=root_id, path="drivers/gpu/after.c"))
        s.commit()


def _prepare_series(seeded):
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    root_id, _url = seeded["root@x"]
    with SessionLocal() as s:
        root = s.get(Article, root_id)
        root.patch_series_key = "serieskey"
        root.patch_series_version = "v1"
        root.patch_series_position = 0
        s.commit()


def _mutate_series(seeded):
    """A v2 is posted. It supersedes this article's badge and rewrites
    its synthesis prose to "revision 1 of 2", but the newer revision is
    a DIFFERENT article in a different thread, so it moves neither the
    node count, the thread's max date, the landing state, the trailer
    count, nor the MAINTAINERS cursor. Posting a v2 is the most routine
    patch workflow on the list."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    reply_id, _url = seeded["reply@x"]
    with SessionLocal() as s:
        other = s.get(Article, reply_id)
        other.patch_series_key = "serieskey"
        other.patch_series_version = "v2"
        other.patch_series_position = 0
        s.commit()


# Each case is (label, prepare | None, mutate). Every mutation changes
# something both surfaces RENDER while touching nothing else either
# ETag covers.
_RENDER_STATE_CASES = [
    ("mainline_landing", None, _mutate_mainline_landing),
    ("second_landing", _prepare_first_landing, _mutate_second_landing),
    (
        "relanded_rebasing_tree",
        _prepare_first_landing,
        _mutate_relanded_in_rebasing_tree,
    ),
    ("maintainers_reparse", _prepare_rules_version, _mutate_rules_version),
    ("review_trailer", None, _mutate_review_trailer),
    ("article_files_rewrite", _prepare_article_file, _mutate_article_file),
    ("newer_revision", _prepare_series, _mutate_series),
]


@pytest.mark.parametrize("surface", ["message", "thread"])
@pytest.mark.parametrize(
    "case", _RENDER_STATE_CASES, ids=[c[0] for c in _RENDER_STATE_CASES]
)
def test_render_state_moves_the_etag_on_both_surfaces(client, tmp_path, surface, case):
    """Derived state must move the validator on EVERY surface showing it.

    Both routes are served `public, no-cache`, so the ETag is the only
    thing standing between a source-data change and an edge (plus every
    crawler) serving a body the data has already contradicted. Both
    render the same four derived surfaces: subsystem attribution,
    lifecycle badge, review roll-up, revisions fold. None of them moves
    an article id, a node count or a thread's max date.

    Parametrised over BOTH surfaces deliberately. The thread view had
    per-input guards and the message page had no state input at all,
    which is precisely the shape a single-surface guard cannot see: it
    confirms the implementation it was written against rather than the
    invariant. See each mutation's own docstring for why that input is
    load-bearing.
    """
    from sqlalchemy import delete

    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry
    from tests.test_routes._helpers import seed_thread_shape

    _label, prepare, mutate = case
    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("reply@x", "root@x")]
    )
    _root_id, root_url = seeded["root@x"]
    url = root_url if surface == "message" else root_url + "/t"

    if prepare is not None:
        prepare(seeded)
    before = client.get(url)
    assert before.status_code == 200, f"{surface} did not render"

    mutate(seeded)
    # Both pages read cached derived state (lifecycle is keyed per
    # ARTICLE, so an inbox-scoped purge would miss it). Production
    # invalidates explicitly on each of these writes; the ETag itself
    # is computed from direct reads either way.
    with SessionLocal() as s:
        s.execute(delete(CacheEntry))
        s.commit()

    after = client.get(url)
    assert after.status_code == 200
    assert before.headers["ETag"] != after.headers["ETag"], (
        f"{surface} page ETag did not move on {_label}"
    )


def test_single_message_rule_agrees_between_message_page_and_thread_view(
    client,
    tmp_path,
):
    """The two surfaces must count the same thread.

    `thread.py` de-duplicates nodes (a self-referential In-Reply-To
    makes the recursive CTE re-emit one article per level), so a
    self-parent renders ONE message. `message.py` gates consolidation
    on the same walk, and if it counts the UNDEDUPED rows it sees 1001
    and consolidates onto a `/t` that shows a single message and drops
    the message page's attachments and related surfaces, i.e. exactly
    the trade the single-message rule exists to prevent. Unlike the
    containment check, `len()` has no safe direction.
    """
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("sp1@x", "sp1@x")])
    _aid, url = seeded["sp1@x"]

    canonical = _canonical_of(client.get(url).get_data(as_text=True))
    rendered = client.get(url + "/t").get_data(as_text=True)
    count = rendered.count('class="thread-message"')

    assert count == 1
    assert canonical.endswith(url), (
        f"a {count}-message thread view must not be the canonical target"
    )


@pytest.mark.parametrize("listing", ["/alpha/", "/alpha/yesterday"])
def test_listings_link_the_reply_count_to_the_thread_view(client, tmp_path, listing):
    """The thread view needs inbound links, not just a canonical.

    Every message in a multi-message thread canonicalises to `/t`, the
    sitemap lists `/t`, and IndexNow announces `/t`. Yet before this the
    whole template set contained exactly ONE link to it (a conditional
    one on the message page), while ten templates linked message pages.
    Internal links are how crawl authority actually flows, so a
    canonical target reachable only through the pages that disclaim
    themselves is a weak structure whatever the canonical tag says.

    Parametrised over both listings that already carry a reply count.
    Guarding only the inbox dashboard would have left the daily view
    free to drop its link silently, which is what a first pass here
    did: same held-fixed-axis mistake as the ETag guards.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from mimir.extensions import SessionLocal
    from mimir.models import Article
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("hub@x", None), ("hub2@x", "hub@x")]
    )
    root_id, _url = seeded["hub@x"]

    # `seed_thread_shape` stamps a fixed 2024-01-01 commit time, and
    # `articles.date` IS the commit time (CONTEXT.md), so the thread
    # sits outside both the 7-day activity window and any day view.
    # Noon YESTERDAY is unambiguously inside both whatever time the
    # suite runs at, unlike `now - 1h`, which straddles midnight UTC.
    when = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    with SessionLocal() as s:
        for mid in ("hub@x", "hub2@x"):
            s.execute(
                update(Article).where(Article.message_id == mid).values(date=when)
            )
        s.commit()
    root_url = f"/alpha/{when.year}/{when.month:02d}/{root_id}"

    html = client.get(listing).get_data(as_text=True)
    # Assert on the ANCHOR, not on the URL appearing anywhere. The
    # inbox page's JSON-LD ItemList already carries thread-view URLs,
    # so a substring check passes with the link removed entirely: it
    # would test the structured data, not the link graph.
    assert f'<a href="{root_url}/t"' in html, f"{listing} does not LINK the thread view"


def test_single_message_threads_are_not_linked_to_their_thread_view(client, tmp_path):
    """The other direction, and the one that keeps the link honest.

    `/t` on a single-message thread is the POORER page (it drops the
    attachments, related surfaces and lifecycle prose the message page
    carries), which is exactly why it is deliberately not that
    message's canonical. Advertising it from a listing would push
    crawlers at a page the site itself declines to nominate.
    """
    from tests.test_routes._helpers import _ingest_one_article

    _aid, url = _ingest_one_article(tmp_path, "alpha", "lonely@example.com")

    html = client.get("/alpha/").get_data(as_text=True)
    assert url in html, "the message itself should still be listed"
    assert f'<a href="{url}/t"' not in html, "linked /t for a thread with no replies"


@pytest.mark.parametrize(
    "path_suffix,expected",
    [("/t/0", 404), ("/t/99999999999999999999", 404), ("/t/10001", 404)],
)
def test_out_of_range_pages_are_rejected(client, tmp_path, path_suffix, expected):
    """Page bounds are validated before the number reaches SQL.

    An astronomically large page became an OFFSET and 500'd the month
    sitemap on a public URL once; this route copied the bound and
    nothing pinned it, so a mutation deleting the check survived the
    whole suite.
    """
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]
    assert client.get(root_url + path_suffix).status_code == expected


def test_explicit_page_one_redirects_to_the_bare_url(client, tmp_path):
    """`/t/1` was a second crawlable URL for page 1.

    Werkzeug's int converter is `\\d+` then `int()`, so `/t/01`,
    `/t/001` and so on are an unbounded family of duplicates. The
    redirect target is built by hand because `url_for` renders the
    month unpadded, which would swap one duplicate spelling for
    another.
    """
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]

    for suffix in ("/t/1", "/t/01"):
        resp = client.get(root_url + suffix)
        assert resp.status_code == 301, f"{suffix} did not redirect"
        assert resp.headers["Location"].endswith(root_url + "/t"), (
            f"{suffix} redirected to {resp.headers['Location']}"
        )
    # The bare URL must NOT redirect to itself.
    assert client.get(root_url + "/t").status_code == 200


def test_each_page_gets_its_own_etag_and_title(client, tmp_path, monkeypatch):
    """Pages are separate self-canonical documents, so they need
    separate validators and separate titles.

    Both were missing: `page` was absent from the ETag input, so page 2
    answered 304 to page 1's validator; and every page shipped a
    byte-identical `<title>` and description while each was
    self-canonical and separately sitemapped, which is a duplicate-title
    signal on every URL the pagination adds.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    edges = [("t0@x", None)] + [(f"t{i}@x", "t0@x") for i in range(1, 6)]
    seeded = seed_thread_shape(tmp_path, "alpha", edges)
    _root_id, root_url = seeded["t0@x"]

    seen_etags, seen_titles = set(), set()
    for page in ("/t", "/t/2", "/t/3"):
        resp = client.get(root_url + page)
        assert resp.status_code == 200, f"{page} -> {resp.status_code}"
        html = resp.get_data(as_text=True)
        seen_etags.add(resp.headers["ETag"])
        seen_titles.add(re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip())

    assert len(seen_etags) == 3, f"pages share validators: {seen_etags}"
    assert len(seen_titles) == 3, f"pages share titles: {seen_titles}"


def test_pages_past_the_first_carry_no_forum_structured_data(
    client, tmp_path, monkeypatch
):
    """Only page 1 holds the root's body.

    A `DiscussionForumPosting` on page 2 would be rooted at a post that
    page does not contain. Nothing pinned this, so emitting it
    everywhere survived the suite.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    edges = [("u0@x", None)] + [(f"u{i}@x", "u0@x") for i in range(1, 5)]
    seeded = seed_thread_shape(tmp_path, "alpha", edges)
    _root_id, root_url = seeded["u0@x"]

    page1 = _json_ld_blocks(client.get(root_url + "/t").data.decode())
    assert any(
        g["@type"] == "DiscussionForumPosting"
        for block in page1
        for g in block.get("@graph", [])
    ), "page 1 lost its forum structured data"

    for later in ("/t/2", "/t/3"):
        blocks = _json_ld_blocks(client.get(root_url + later).data.decode())
        assert not any(
            g["@type"] == "DiscussionForumPosting"
            for block in blocks
            for g in block.get("@graph", [])
        ), f"{later} claims to be the forum thread but lacks the root"


def test_thread_view_sends_vary_hx_request(client, tmp_path):
    """Two representations at one URL need the header that says so.

    The route serves a partial under `HX-Request: true` and the page
    otherwise. `hooks.py` gated this on the message endpoint only, while
    two comments in the thread route asserted it was being set here.
    Divergent ETags cover any cache that revalidates; bfcache and
    prerender do not.
    """
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _, root_url, _ = seeded["root"]

    full = client.get(root_url + "/t")
    partial = client.get(root_url + "/t", headers={"HX-Request": "true"})
    assert full.headers.get("Vary") == "HX-Request"
    assert partial.headers.get("Vary") == "HX-Request"
    assert full.headers["ETag"] != partial.headers["ETag"], (
        "the two representations share a validator"
    )
    assert "<h1" in full.get_data(as_text=True)
    assert "<h1" not in partial.get_data(as_text=True), (
        "the HTMX fragment carries page chrome"
    )


def test_one_dateless_message_does_not_strip_the_thread_canonical(
    client, tmp_path, monkeypatch
):
    """The root is taken by IDENTITY, not by position.

    `get_thread`'s `sort_path` is NULL for a dateless node and NULL sorts
    first, so `target_thread[0]` was that node rather than the root, its
    `date` was None, and the whole consolidation block was skipped: ONE
    dateless message stripped the thread canonical from every message in
    its thread. Reverting the fix passed the entire suite.
    """
    from sqlalchemy import select, update

    from mimir.config import settings
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    monkeypatch.setattr(settings, "thread_view_render_cap", 50)
    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [("g0@x", None), ("g1@x", "g0@x"), ("g2@x", "g1@x"), ("g3@x", "g2@x")],
    )
    root_id, root_url = seeded["g0@x"]

    with SessionLocal() as s:
        s.execute(
            update(Article).where(Article.id == seeded["g2@x"][0]).values(date=None)
        )
        s.commit()
        assert (
            s.execute(
                select(Article.date).where(Article.id == seeded["g2@x"][0])
            ).scalar_one()
            is None
        ), "fixture did not null the date"

    # The dateless message is not addressable (its URL renders as
    # 0000/00), but every OTHER message must still consolidate.
    for mid in ("g0@x", "g1@x", "g3@x"):
        _, msg_url = seeded[mid]
        html = client.get(msg_url).get_data(as_text=True)
        canonical = _canonical_of(html)
        assert canonical is not None and canonical.endswith(root_url + "/t"), (
            f"{mid} lost its thread canonical because another message in "
            f"the thread has no date; got {canonical}"
        )
