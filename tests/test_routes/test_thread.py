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
    """A MAINTAINERS reparse, which `update-mainline` performs every 10
    minutes in prod. It rewrites which subsystems claim the article and
    adds no message, so it moves neither node count nor thread max
    date. Versioned through `MainlineState.last_commit_sha` (the HEAD
    at the last load) so one scalar covers the whole rule snapshot
    rather than re-deriving this article's matches before the 304."""
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
