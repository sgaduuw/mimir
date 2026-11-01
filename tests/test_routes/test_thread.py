"""Tests for mimir/web/routes/thread.py: the whole-thread view, plus
the message page's canonical consolidation onto it."""

import json
import re
from datetime import UTC

import pytest

from mimir.config import settings
from mimir.models import Article
from tests.test_routes._helpers import (
    _clear_sitemap_cache,
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


def test_thread_view_json_ld_hides_addresses_the_page_hides(
    client, tmp_path, monkeypatch
):
    """Root and comment `text` hide every address the page hides
    (#899): the attribution line and a quoted trailer, while an
    allowlisted trailer address stays."""
    monkeypatch.setattr(settings, "email_allowlist", ["@kernel.org"])
    body = (
        b"On Mon, Joe User <joe@example.com> wrote:\n"
        b"> Signed-off-by: Joe User <joe@example.com>\n"
        b"\n"
        b"my reply\n"
        b"\n"
        b"Signed-off-by: Maintainer <m@kernel.org>\n"
    )
    assert b"joe@example.com" in body  # precondition
    seeded = _seed_three_message_thread(tmp_path, "alpha", bodies=(body, body, b"x"))
    _, root_url, _ = seeded["root"]
    blocks = _json_ld_blocks(client.get(root_url + "/t").data.decode())
    posting = next(
        g for g in blocks[0]["@graph"] if g["@type"] == "DiscussionForumPosting"
    )
    texts = [posting["text"], posting["comment"][0]["text"]]
    for text in texts:
        assert "my reply" in text  # precondition: root and reply bodies
        assert "m@kernel.org" in text
    assert "joe@example.com" not in json.dumps(blocks)


def _comment_authors(comments):
    """Every Comment author in a (nested) JSON-LD comment tree."""
    for c in comments:
        yield c["author"]
        yield from _comment_authors(c.get("comment", []))


def test_thread_view_json_ld_nameless_sender_never_unknown_sender(
    client,
    tmp_path,
    monkeypatch,
):
    """A From: with no display name must not become the Person.name
    "unknown sender" (#845). Allowlisted: the address is the name (it
    is already public on the page). Not allowlisted: `author` stays
    (Google requires it) with no `name`, and the address appears
    nowhere in the JSON-LD."""
    monkeypatch.setattr(settings, "email_allowlist", ["@open.example"])
    seeded = _seed_three_message_thread(
        tmp_path,
        "alpha",
        authors=(
            "Ada Lovelace <ada@hidden.example>",
            "ok@open.example",
            "secret@hidden.example",
        ),
    )
    html = client.get(seeded["root"][1] + "/t").data.decode()
    graph = _json_ld_blocks(html)[0]["@graph"]
    posting = next(g for g in graph if g["@type"] == "DiscussionForumPosting")
    authors = list(_comment_authors(posting["comment"]))
    # Precondition: both replies are really in the (nested) comment tree.
    assert len(authors) == 2
    assert posting["author"]["name"] == "Ada Lovelace"
    named = [a.get("name") for a in authors]
    assert "ok@open.example" in named
    anon = next(a for a in authors if "name" not in a)
    assert anon["@type"] == "Person"
    ld = json.dumps(graph)
    assert "unknown sender" not in ld
    assert "secret@hidden.example" not in ld


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


@pytest.mark.parametrize("inbox_name", ["alpha", "beta"])
def test_flat_view_link_reaches_the_message_page(
    client, tmp_path, monkeypatch, inbox_name
):
    """The real link follows canonical inbox and page selection, including replies."""
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = _seed_three_message_thread(tmp_path, "alpha")
    _cross_post(seeded, ["root", "reply", "nested"])
    root_url = seeded["root"][1]
    destinations = [root_url + "/t", root_url + "/t", root_url + "/t/2"]
    assert len(seeded) == len(destinations) == 3
    for (article_id, url, _), destination in zip(
        seeded.values(), destinations, strict=True
    ):
        response = client.get(url.replace("/alpha/", f"/{inbox_name}/"))
        assert response.status_code == 200
        link = re.search(
            r'<a href="([^"]+)">flat view</a>', response.get_data(as_text=True)
        )
        assert link, "message page lost its flat-view link"
        assert link.group(1) == destination
        thread = client.get(destination)
        assert thread.status_code == 200
        assert f'id="m{article_id}"' in thread.get_data(as_text=True)


def test_single_message_thread_canonicalises_to_its_thread_view(client, tmp_path):
    """Every thread's aggregations live on its `/t` (#1003), so a
    one-message thread's message page names that `/t`, as a reply's does."""
    _, url = _ingest_one_article(tmp_path, "alpha", "solo@example.com")
    html = client.get(url).get_data(as_text=True)
    assert _canonical_of(html) == f"http://localhost{url}/t"
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
    resolves against BETA's copy of the conversation. `_cross_post` adds
    beta's row with no `thread_root_id`, so the column cannot rank the
    reply there (`unmaterialised_roots` condition 1) and the canonical is
    beta's message page rather than any thread view. What matters is the
    invariant, not which URL wins: it resolves, contains the message, and
    is terminal.
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
    """Every message page in the thread canonicalises here (#1003), so
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

    A single-message thread's canonical is its `/t`, like every thread's
    (#1003), so the sitemap lists that `/t` and not the message page.
    Listing the other would publish a URL whose own page names a
    different canonical.

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
    _clear_sitemap_cache()

    # The year's archive files, both trees: they list every root (#964).
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    year = seeded["solo-sm@x"][1].split("/")[2]
    locs = set()
    for name in ("sitemap.xml", "messages-sitemap.xml"):
        root = ET.fromstring(client.get(f"/alpha/{year}/{name}").get_data())
        locs |= {u.find("s:loc", ns).text.replace("http://localhost", "") for u in root}

    checked = 0
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
        checked += 1
    # The two roots, solo and m1; the reply m2 is listed nowhere.
    assert checked == 2, f"only {checked} of the seeded roots were listed"


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
    from mimir.models import ArticleFile, MainlineState

    root_id, _url = seeded["root@x"]
    with SessionLocal() as s:
        s.add(MainlineState(tree_name="linus", last_commit_sha="a" * 40))
        s.add(ArticleFile(article_id=root_id, path="drivers/net/x.c"))
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
    from mimir.subsystems import invalidate_rules_snapshot
    from tests.test_routes._helpers import _seed_subsystem

    # The reload adds a subsystem claiming the article's file, and
    # drops the rules snapshot as `load_maintainers` does.
    _seed_subsystem("NETWORKING DRIVERS", "Maintained", ["drivers/net/"])
    with SessionLocal() as s:
        s.get(MainlineState, "linus").last_commit_sha = "b" * 40
        s.commit()
    invalidate_rules_snapshot()


def _mutate_mainline_landing(seeded):
    """The patch lands in Linus's tree. Flips the lifecycle badge to
    LANDED and rewrites the "did $series land" prose, while adding no
    message to the thread. Same class as the 3.6.1 sitemap incident: a
    validator that cannot see the change pins the wrong answer at the
    edge and in every crawler until a deploy bumps the version."""
    from datetime import datetime

    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.add(
            MainlineCommit(
                commit_sha="a" * 40,
                message_id="root@x",
                tree_name="linus",
                committed_at=datetime(2024, 6, 1, tzinfo=UTC),
            )
        )
        s.commit()


def _prepare_first_landing(seeded):
    from datetime import datetime

    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.add(
            MainlineCommit(
                commit_sha="c" * 40,
                message_id="root@x",
                tree_name="linux-next",
                committed_at=datetime(2024, 6, 1, tzinfo=UTC),
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
    from datetime import datetime

    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.add(
            MainlineCommit(
                commit_sha="d" * 40,
                message_id="root@x",
                tree_name="linus",
                # Deliberately NOT newer, so only the count moves.
                committed_at=datetime(2024, 5, 1, tzinfo=UTC),
            )
        )
        s.commit()


def _mutate_relanded_in_rebasing_tree(seeded):
    """A rebasing tree (`linux-next`, `rebases=True`) is re-walked daily
    and its rows replaced, so the same message reappears under a new
    commit sha with a new committer date at UNCHANGED count. This is
    the case that moves only the max, and the only one that pins it."""
    from datetime import datetime

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
                committed_at=datetime(2024, 9, 9, tzinfo=UTC),
            )
        )
        s.commit()


def _mutate_queued_to_landed_via_next(seeded):
    """Linus merges a commit linux-next already carried, then
    linux-next's daily rebuild drops its row because it now skips
    commits Linus has (#673). Same SHA, same committed_at, one row
    before and one after: the pill goes from "in linux-next" to
    LANDED while a count-and-max tag stays identical, so a client
    holding the queued page got a 304 for a landed patch."""
    from datetime import datetime

    from sqlalchemy import delete

    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.execute(
            delete(MainlineCommit).where(
                MainlineCommit.message_id == "root@x",
                MainlineCommit.tree_name == "linux-next",
            )
        )
        s.add(
            MainlineCommit(
                commit_sha="c" * 40,
                message_id="root@x",
                tree_name="linus",
                committed_at=datetime(2024, 6, 1, tzinfo=UTC),
            )
        )
        s.commit()


def _prepare_linus_landing(seeded):
    from datetime import datetime

    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.add(
            MainlineCommit(
                commit_sha="a" * 40,
                message_id="root@x",
                tree_name="linus",
                committed_at=datetime(2024, 6, 1, tzinfo=UTC),
            )
        )
        s.commit()


def _mutate_second_commit_same_tree(seeded):
    """A second Linus commit names the same patch, OLDER than the first,
    so the newest date stays put and the tree SET stays {linus}: only the
    duplicate in the tree list moves. The tooltip and the landing
    sentence change, so the validator must keep duplicates, not a set."""
    from datetime import datetime

    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.add(
            MainlineCommit(
                commit_sha="f" * 40,
                message_id="root@x",
                tree_name="linus",
                committed_at=datetime(2024, 5, 1, tzinfo=UTC),
            )
        )
        s.commit()


def _mutate_rebased_same_date(seeded):
    """linux-next rebuilds its row under a NEW sha with the SAME
    committer date (a rebase that keeps committer dates). Tree list and
    dates are unchanged; the tooltip and the "Queued in linux-next as
    <sha>" sentence show the new sha, so the validator must carry it."""
    from datetime import datetime

    from sqlalchemy import delete

    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit

    with SessionLocal() as s:
        s.execute(delete(MainlineCommit).where(MainlineCommit.message_id == "root@x"))
        s.add(
            MainlineCommit(
                commit_sha="9" * 40,
                message_id="root@x",
                tree_name="linux-next",
                committed_at=datetime(2024, 6, 1, tzinfo=UTC),
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


def _prepare_review_trailer(seeded):
    _mutate_review_trailer(seeded)


def _mutate_trailer_in_place(seeded):
    """`backfill-article-trailers --reprocess` after a parser fix
    rewrites a reviewer's name in place. The row count stays put, and
    the pill tooltip shows the new name."""
    from mimir.extensions import SessionLocal
    from mimir.models import ArticleTrailer

    root_id, _url = seeded["root@x"]
    with SessionLocal() as s:
        s.query(ArticleTrailer).filter_by(
            article_id=root_id
        ).one().name = "Rita Eviewer"
        s.commit()


def _prepare_article_file(seeded):
    from mimir.extensions import SessionLocal
    from mimir.models import ArticleFile
    from tests.test_routes._helpers import _seed_subsystem

    _seed_subsystem("NETWORKING DRIVERS", "Maintained", ["drivers/net/"])
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


def _prepare_two_revisions(seeded):
    _prepare_series(seeded)
    _mutate_series(seeded)


def _mutate_series_head(seeded):
    """A re-chain moves a whole series under a head id (#816):
    an older unrelated posting under the same key takes the NULL head.
    Every revision moves together, so the revision rows the tag digests
    are the same rows, while the fold's diff link gains `s=`."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    root_id, _url = seeded["root@x"]
    reply_id, _url = seeded["reply@x"]
    with SessionLocal() as s:
        for aid in (root_id, reply_id):
            s.get(Article, aid).patch_series_head_id = root_id
        s.commit()


def _prepare_slot_rows(seeded):
    """The thread's two articles become two rows of one series slot,
    neither with a diff, beside a v2 so the page carries a revision
    sentence (#861)."""
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        for mid in ("root@x", "reply@x"):
            art = s.get(Article, seeded[mid][0])
            art.patch_series_key = "slotkey"
            art.patch_series_version = "v1"
            art.patch_series_position = 1
        s.add(
            Article(
                message_id="v2@x",
                subject="[PATCH v2] foo: bar",
                author="A <a@x>",
                patch_series_key="slotkey",
                patch_series_version="v2",
                patch_series_position=1,
            )
        )
        s.commit()


def _mutate_slot_diff(seeded):
    """The later row of the slot gains a diff, which makes it the slot's
    patch and the viewed (earlier) root a no-diff row that lost: not a
    patch, so no card and no revision sentence. Its path is no input of
    the viewed article, so the files tag, the series columns and the
    revision ids do not move; only the has-diff of a sibling does."""
    from mimir.extensions import SessionLocal
    from mimir.models import ArticleFile

    with SessionLocal() as s:
        s.add(ArticleFile(article_id=seeded["reply@x"][0], path="fs/foo.c"))
        s.commit()


def test_newer_revision_renders_the_superseded_pill(client, tmp_path):
    """Stored versions are `vN`, which SQLite casts to 0, so for a
    while no page ever showed SUPERSEDED (#677)."""
    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("reply@x", "root@x")]
    )
    with SessionLocal() as s:
        s.get(Article, seeded["root@x"][0]).subject = "[PATCH] foo: bar"
        s.commit()
    _prepare_series(seeded)
    _mutate_series(seeded)
    html = client.get(seeded["root@x"][1]).get_data(as_text=True)
    assert "Superseded by v2 posted" in html
    assert re.search(r"\bSUPERSEDED\b", html)


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
    (
        "queued_to_landed_via_next",
        _prepare_first_landing,
        _mutate_queued_to_landed_via_next,
    ),
    ("rebased_same_date", _prepare_first_landing, _mutate_rebased_same_date),
    (
        "second_commit_same_tree",
        _prepare_linus_landing,
        _mutate_second_commit_same_tree,
    ),
    ("maintainers_reparse", _prepare_rules_version, _mutate_rules_version),
    ("review_trailer", None, _mutate_review_trailer),
    ("article_files_rewrite", _prepare_article_file, _mutate_article_file),
    ("trailer_rewrite", _prepare_review_trailer, _mutate_trailer_in_place),
    ("newer_revision", _prepare_series, _mutate_series),
    ("series_head", _prepare_two_revisions, _mutate_series_head),
    ("slot_row_gains_a_diff", _prepare_slot_rows, _mutate_slot_diff),
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
    # Cleared so this test asks only whether the VALIDATOR moves. Do not
    # read it as "production invalidates on these writes": it does not
    # for landings, which is why both pages compute the lifecycle pill
    # uncached (pinned by `test_etag_page_does_not_render_a_cached_pill`).
    with SessionLocal() as s:
        s.execute(delete(CacheEntry))
        s.commit()

    after = client.get(url)
    assert after.status_code == 200
    assert before.headers["ETag"] != after.headers["ETag"], (
        f"{surface} page ETag did not move on {_label}"
    )


@pytest.mark.parametrize("surface", ["message", "thread"])
def test_etag_page_does_not_render_a_cached_pill(client, tmp_path, surface):
    """The body must show the state the validator was minted from.

    The ETag reads the landing rows live. When the page took its pill
    from the 5-minute cache, a response could carry the NEW validator
    beside the OLD pill; when the cache row expired the body changed
    under an ETag that did not, and every cache holding that response
    kept the stale pill for good. Nothing invalidates the pill when a
    landing row is written, so the cache here is primed the way a
    listing page primes it in production, not cleared.
    """
    from datetime import datetime

    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.lifecycle_status import lifecycle_status_for_articles
    from mimir.models import Inbox, MainlineCommit
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("reply@x", "root@x")]
    )
    root_id, root_url = seeded["root@x"]
    url = root_url if surface == "message" else root_url + "/t"
    with SessionLocal() as s:
        # Pills render only on patches, keyed off a `[PATCH ...]` subject.
        s.get(Article, root_id).subject = "[PATCH] foo: bar"
        s.add(
            MainlineCommit(
                commit_sha="c" * 40,
                message_id="root@x",
                tree_name="linux-next",
                committed_at=datetime(2024, 6, 1, tzinfo=UTC),
            )
        )
        s.commit()
        inbox_id = s.scalar(select(Inbox.id).where(Inbox.name == "alpha"))
        primed = lifecycle_status_for_articles(s, [root_id], inbox_id)[root_id]
    assert primed.pill_label == "IN LINUX-NEXT", primed.pill_label  # precondition

    with SessionLocal() as s:
        s.add(
            MainlineCommit(
                commit_sha="c" * 40,
                message_id="root@x",
                tree_name="linus",
                committed_at=datetime(2024, 6, 1, tzinfo=UTC),
            )
        )
        s.commit()
        # Precondition: the cache still holds the queued pill.
        still = lifecycle_status_for_articles(s, [root_id], inbox_id)[root_id]
    assert still.pill_label == "IN LINUX-NEXT", still.pill_label

    html = client.get(url).get_data(as_text=True)
    assert "IN LINUX-NEXT" not in html, f"{surface} rendered the cached queued pill"
    assert "LANDED" in html


@pytest.mark.parametrize("surface", ["message", "thread"])
def test_tree_display_name_moves_the_etag(client, tmp_path, monkeypatch, surface):
    """`TREES__<name>__DISPLAY_NAME` feeds "Queued in <label>". A
    restart with a new label keeps the version, so only the validator
    can tell a cache the sentence changed."""
    from mimir.config import settings
    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("reply@x", "root@x")]
    )
    root_id, root_url = seeded["root@x"]
    url = root_url if surface == "message" else root_url + "/t"
    with SessionLocal() as s:
        s.get(Article, root_id).subject = "[PATCH] foo: bar"
        s.commit()
    _prepare_first_landing(seeded)
    before = client.get(url)
    monkeypatch.setattr(settings.trees["linux-next"], "display_name", "next tree")
    after = client.get(url)
    assert before.headers["ETag"] != after.headers["ETag"]
    assert "Queued in next tree" in after.get_data(as_text=True)


@pytest.mark.parametrize("surface", ["message", "thread"])
def test_repeat_request_reuses_the_cached_pill(client, tmp_path, monkeypatch, surface):
    """The pill is cached because computing it walks the whole thread
    (about 120 ms on the largest one). A second request under the same
    ETag must take it from the cache, not compute it again."""
    from mimir import lifecycle_status
    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("reply@x", "root@x")]
    )
    root_id, root_url = seeded["root@x"]
    url = root_url if surface == "message" else root_url + "/t"
    with SessionLocal() as s:
        s.get(Article, root_id).subject = "[PATCH] foo: bar"
        s.commit()
    computed = []
    real = lifecycle_status._bulk_uncached
    monkeypatch.setattr(
        lifecycle_status,
        "_bulk_uncached",
        lambda session, ids, inbox_id: (
            computed.append(ids) or real(session, ids, inbox_id)
        ),
    )
    first = client.get(url)
    assert computed, "precondition: the first request computes the pill"
    computed.clear()
    second = client.get(url)
    assert second.headers["ETag"] == first.headers["ETag"]  # precondition
    assert computed == [], f"{surface} recomputed the pill under an unchanged ETag"


def _synthesis(html: str) -> str:
    """The landing sentence of the patch-state card, or ""."""
    m = re.search(r"((?:Queued in|Landed in mainline as)[^<]*)", html)
    return m.group(1) if m else ""


@pytest.mark.parametrize("surface", ["message", "thread"])
def test_etag_page_does_not_render_a_cached_landing_sentence(client, tmp_path, surface):
    """The patch-state card was cached by article id, out of the
    validator's sight: after a landing the page carried a new ETag and a
    LANDED pill beside "Queued in linux-next", and once the card expired
    the body changed under an unchanged ETag, pinning the stale sentence.
    Primed with an ordinary 200, as production primes it."""
    from datetime import datetime

    from mimir.extensions import SessionLocal
    from mimir.models import MainlineCommit
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("reply@x", "root@x")]
    )
    root_id, root_url = seeded["root@x"]
    url = root_url if surface == "message" else root_url + "/t"
    with SessionLocal() as s:
        s.get(Article, root_id).subject = "[PATCH] foo: bar"
        s.add(
            MainlineCommit(
                commit_sha="c" * 40,
                message_id="root@x",
                tree_name="linux-next",
                committed_at=datetime(2024, 6, 1, tzinfo=UTC),
            )
        )
        s.commit()
    first = client.get(url)
    assert re.match(
        r"Queued in \S+ as c{12}\b", _synthesis(first.get_data(as_text=True))
    )
    with SessionLocal() as s:
        s.query(MainlineCommit).filter_by(
            message_id="root@x", tree_name="linux-next"
        ).delete()
        s.add(
            MainlineCommit(
                commit_sha="c" * 40,
                message_id="root@x",
                tree_name="linus",
                committed_at=datetime(2024, 6, 1, tzinfo=UTC),
            )
        )
        s.commit()
    second = client.get(url)
    assert second.headers["ETag"] != first.headers["ETag"]  # precondition
    html = second.get_data(as_text=True)
    assert re.search(r"\bLANDED\b", html)  # precondition: pill is current
    assert re.match(r"Landed in mainline as c{12}\b", _synthesis(html)), _synthesis(
        html
    )


@pytest.mark.parametrize("surface", ["message", "thread", "thread_md"])
@pytest.mark.parametrize(
    "case", _RENDER_STATE_CASES, ids=[c[0] for c in _RENDER_STATE_CASES]
)
def test_body_under_new_etag_matches_a_cold_render(client, tmp_path, surface, case):
    """For every derived-state change, the page served under the new
    ETag with caches WARM must equal a cold render under that ETag.
    The matrix above clears the cache, so it can only see the
    validator; this one sees a cache the validator cannot."""
    from sqlalchemy import delete

    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry
    from tests.test_routes._helpers import seed_thread_shape

    _label, prepare, mutate = case
    if surface == "thread_md" and _label == "series_head":
        # The head is spelled only in the revisions fold's diff links. The
        # HTML thread view renders that fold (#844), the Markdown one does
        # not; its ETag still moves (shared `render_state_tag`), checked by
        # the matrix above.
        pytest.skip("the Markdown thread page renders no diff link")
    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("reply@x", "root@x")]
    )
    root_id, root_url = seeded["root@x"]
    url = {
        "message": root_url,
        "thread": root_url + "/t",
        "thread_md": root_url + "/t.md",
    }[surface]
    with SessionLocal() as s:
        s.get(Article, root_id).subject = "[PATCH] foo: bar"
        s.commit()
    if prepare is not None:
        prepare(seeded)
    primed = client.get(url)  # prime, do not clear
    assert primed.status_code == 200
    mutate(seeded)
    warm = client.get(url)
    with SessionLocal() as s:
        s.execute(delete(CacheEntry))
        s.commit()
    cold = client.get(url)
    assert warm.headers["ETag"] == cold.headers["ETag"]  # precondition

    def norm(h: str) -> str:
        return re.sub(r'nonce="[^"]*"', 'nonce=""', h)

    # Precondition: the change is visible in the body, or warm == cold
    # holds by construction and guards nothing.
    assert norm(primed.get_data(as_text=True)) != norm(cold.get_data(as_text=True)), (
        f"{surface}: {_label} does not change the rendered page"
    )
    assert norm(warm.get_data(as_text=True)) == norm(cold.get_data(as_text=True)), (
        f"{surface}: warm body differs from cold under one ETag on {_label}"
    )


def _activity(html: str) -> str:
    m = re.search(r'class="badge badge-activity-([a-z]+)"', html)
    return m.group(1) if m else ""


@pytest.mark.parametrize("surface", ["message", "thread"])
def test_reply_does_not_leave_a_cached_activity_chip_under_the_new_etag(
    client, tmp_path, surface
):
    """A reply moves both pages' ETags through the thread's newest date,
    and the activity chip with them. A pill cached under a narrower key
    than the ETag kept the pre-reply chip under the new ETag, then
    flipped to HOT under that same ETag once the row expired."""
    from datetime import datetime

    from sqlalchemy import delete

    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("reply@x", "root@x")]
    )
    root_id, root_url = seeded["root@x"]
    reply_id, _ = seeded["reply@x"]
    url = root_url if surface == "message" else root_url + "/t"
    with SessionLocal() as s:
        s.get(Article, root_id).subject = "[PATCH] foo: bar"
        s.commit()
    first = client.get(url)
    assert _activity(first.get_data(as_text=True)) in {"stale", "dormant"}

    with SessionLocal() as s:
        s.get(Article, reply_id).date = datetime.now(UTC)
        s.commit()

    warm = client.get(url)
    assert warm.headers["ETag"] != first.headers["ETag"]  # precondition
    with SessionLocal() as s:
        s.execute(delete(CacheEntry))
        s.commit()
    cold = client.get(url)
    assert cold.headers["ETag"] == warm.headers["ETag"]  # precondition
    assert _activity(cold.get_data(as_text=True)) == "hot"  # precondition
    assert _activity(warm.get_data(as_text=True)) == "hot", (
        f"{surface}: cached pre-reply chip served under the post-reply ETag"
    )


def _add_reply(inbox_name, message_id, parent, date=None):
    """One reply in `inbox_name` only (default: now), and a cleared cache."""
    from datetime import datetime

    from sqlalchemy import delete, select

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList, CacheEntry, Inbox

    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        reply = Article(
            message_id=message_id,
            thread_parent=parent,
            subject="Re: [PATCH] foo: bar",
            author="c@d.example",
            date=date or datetime.now(UTC),
        )
        s.add(reply)
        s.flush()
        s.add(
            ArticleList(
                article_id=reply.id, inbox_id=inbox.id, epoch=0, commit_sha="b" * 40
            )
        )
        s.execute(delete(CacheEntry))
        s.commit()


@pytest.mark.parametrize("surface", ["message", "thread"])
def test_chip_and_etag_move_together_on_replies(client, tmp_path, surface):
    """The chip reads this inbox's thread, as the ETag does (#676). A
    reply in ANOTHER inbox moves neither; a reply in this one moves both,
    so the agreement is not the chip being stuck."""

    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("root@x", None)])
    root_id, root_url = seeded["root@x"]
    url = root_url if surface == "message" else root_url + "/t"
    with SessionLocal() as s:
        s.get(Article, root_id).subject = "[PATCH] foo: bar"
        s.commit()
    first = client.get(url)
    assert _activity(first.get_data(as_text=True)) == "dormant"  # precondition

    _add_reply("beta", "offlist-reply@x", "root@x")
    off = client.get(url)
    assert _activity(off.get_data(as_text=True)) == "dormant"
    assert off.headers["ETag"] == first.headers["ETag"]

    _add_reply("alpha", "inlist-reply@x", "root@x")
    on = client.get(url)
    assert _activity(on.get_data(as_text=True)) == "hot"
    assert on.headers["ETag"] != first.headers["ETag"]


@pytest.mark.parametrize("surface", ["message", "thread"])
def test_sibling_thread_does_not_move_the_chip_under_the_etag(
    client, tmp_path, surface
):
    """p1 and p2 reply to a cover letter this inbox never got, so each is
    its own thread here. Once the cover lands in another inbox, a reply to
    p2 must not move p1's chip: p1's page and ETag do not contain it
    (#676)."""
    from datetime import datetime

    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("p1@x", "cover@x"), ("p2@x", "cover@x")]
    )
    p1_id, p1_url = seeded["p1@x"]
    url = p1_url if surface == "message" else p1_url + "/t"
    with SessionLocal() as s:
        s.get(Article, p1_id).subject = "[PATCH] foo: bar"
        s.commit()
    first = client.get(url)
    assert _activity(first.get_data(as_text=True)) == "dormant"  # precondition

    _add_reply("beta", "cover@x", None, datetime(2024, 1, 1, tzinfo=UTC))
    _add_reply("alpha", "p2-reply@x", "p2@x")

    after = client.get(url)
    assert after.headers["ETag"] == first.headers["ETag"]  # precondition
    assert _activity(after.get_data(as_text=True)) == "dormant"


@pytest.mark.parametrize("surface", ["message", "thread"])
def test_chip_counts_own_replies_below_an_off_inbox_parent(client, tmp_path, surface):
    """top (alpha) <- mid (beta only) <- leaf (alpha). In alpha, leaf is
    its own thread and its page shows the fresh reply, so the chip is hot.
    Walking up through mid to top would start the inbox-scoped walk down
    where it cannot pass mid, and lose leaf's own reply (#676)."""
    from datetime import datetime

    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("top@x", None), ("leaf@x", "mid@x")]
    )
    leaf_id, leaf_url = seeded["leaf@x"]
    url = leaf_url if surface == "message" else leaf_url + "/t"
    with SessionLocal() as s:
        s.get(Article, leaf_id).subject = "[PATCH] foo: bar"
        s.commit()
    _add_reply("beta", "mid@x", "top@x", datetime(2024, 1, 1, tzinfo=UTC))
    _add_reply("alpha", "leaf-reply@x", "leaf@x")

    assert _activity(client.get(url).get_data(as_text=True)) == "hot"


@pytest.mark.parametrize("surface", ["message", "thread"])
def test_rebased_subsystem_row_below_the_max_moves_the_etag(client, tmp_path, surface):
    """Queued in mm (older) and linux-next (newer); mm rebases to a new
    sha still older than linux-next's row. The tree list and the newest
    date stay put while the tooltip and the "Queued in mm as <sha>"
    sentence change, so the validator tags the rows themselves."""
    from datetime import datetime

    from sqlalchemy import delete

    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry, MainlineCommit
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("root@x", None)])
    root_id, root_url = seeded["root@x"]
    url = root_url if surface == "message" else root_url + "/t"
    with SessionLocal() as s:
        s.get(Article, root_id).subject = "[PATCH] foo: bar"
        s.add(
            MainlineCommit(
                commit_sha="1" * 40,
                message_id="root@x",
                tree_name="mm",
                committed_at=datetime(2024, 6, 1, tzinfo=UTC),
            )
        )
        s.add(
            MainlineCommit(
                commit_sha="2" * 40,
                message_id="root@x",
                tree_name="linux-next",
                committed_at=datetime(2024, 7, 1, tzinfo=UTC),
            )
        )
        s.commit()
    first = client.get(url)
    assert "111111111111" in first.get_data(as_text=True)  # precondition
    with SessionLocal() as s:
        s.execute(delete(MainlineCommit).where(MainlineCommit.tree_name == "mm"))
        s.add(
            MainlineCommit(
                commit_sha="3" * 40,
                message_id="root@x",
                tree_name="mm",
                committed_at=datetime(2024, 6, 15, tzinfo=UTC),
            )
        )
        s.execute(delete(CacheEntry))
        s.commit()
    second = client.get(url)
    assert "333333333333" in second.get_data(as_text=True)  # precondition
    assert second.headers["ETag"] != first.headers["ETag"], (
        f"{surface}: body moved, ETag did not"
    )


def test_a_self_parented_message_names_a_thread_view_that_holds_it_once(
    client,
    tmp_path,
):
    """A self-referential In-Reply-To makes the recursive CTE re-emit one
    article per level, so `thread.py` de-duplicates nodes and renders ONE
    message. Since #1003 a one-message thread canonicalises to its `/t`
    too, so the claim to check is containment: the canonical names the
    `/t`, and that page renders this message exactly once.
    """
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("sp1@x", "sp1@x")])
    _aid, url = seeded["sp1@x"]

    canonical = _canonical_of(client.get(url).get_data(as_text=True))
    rendered = client.get(url + "/t").get_data(as_text=True)
    count = rendered.count('class="thread-message"')

    assert count == 1
    assert canonical == f"http://localhost{url}/t"


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
    from datetime import datetime, timedelta

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
    when = (datetime.now(UTC) - timedelta(days=1)).replace(
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
    """The listing links its reply count, so a thread with no replies has
    no count to link. Since #1003 that `/t` is the message's canonical and
    is reached through the message page's own thread link; whether a
    listing row should link it too is the listing redesign's call (#991).
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
        seen_titles.add(
            re.search(r"<title>(.*?)</title>", html, re.DOTALL).group(1).strip()
        )

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


def test_json_ld_survives_a_parent_cycle_in_the_thread_graph():
    """A `thread_parent` cycle must not produce an unserialisable
    payload.

    The nesting pass reparents live dicts, so if two messages named each
    other as parent the object graph would contain a cycle and
    `json.dumps` would raise `ValueError: Circular reference detected`,
    500ing the one URL every message in the thread canonicalises to.

    The claim is that this cannot happen: every member of a cycle has
    its parent present in the document, so no member is ever appended to
    the top-level list, and nothing reachable from the payload points
    into the cycle. That is a structural argument about code I wrote,
    which is exactly the kind this branch has repeatedly found wanting,
    so it is asserted rather than reasoned. The cost of being wrong is a
    hard 500; the cost of the test is one dict.

    The cycle is unreachable, not repaired: those messages are absent
    from the JSON-LD. That is the correct trade (a corrupt thread loses
    machine-readable comments, the page still serves) and is asserted
    below so a future "fix" that reattaches them has to confront the
    serialisation question deliberately.
    """
    import json
    from datetime import datetime

    from mimir.seo.json_ld import _json_ld_thread
    from mimir.threading import ThreadNode

    def node(n, parent):
        return ThreadNode(
            id=n,
            message_id=f"<m{n}@example.test>",
            thread_parent=parent,
            subject=f"subject {n}",
            author="A <a@example.test>",
            # Dated: comment URLs carry the year/month, so a dateless
            # node would make the assertions below match nothing and
            # pass for the wrong reason.
            date=datetime(2024, 1, 5, tzinfo=UTC),
            depth=None,
        )

    payload = _json_ld_thread(
        # 2 and 3 name each other; 4 is an ordinary reply to the root.
        nodes=[
            node(1, None),
            node(2, "<m3@example.test>"),
            node(3, "<m2@example.test>"),
            node(4, "<m1@example.test>"),
        ],
        parsed_by_id={},
        canonical_url="https://example.test/alpha/2024/01/1/t",
        inbox_name="alpha",
        base="https://example.test",
        total_replies=3,
        subsystem_names=[],
    )

    encoded = json.dumps(payload)
    assert "/2024/01/4" in encoded, "the ordinary reply is still described"
    for cyclic in ("/2024/01/2", "/2024/01/3"):
        assert cyclic not in encoded, (
            f"{cyclic} is reachable from the payload, so the cycle is now "
            "serialised and the next corrupt thread 500s"
        )


def test_no_source_comment_still_claims_the_overflow_containment_gate(client, tmp_path):
    """The two routes must not still DESCRIBE the behaviour 3.8.0
    removed.

    Property pinned: for every claim this branch invalidated, the code
    that carried the claim carries the current one instead. Concretely,
    "past the render cap the thread view only LINKS to a message rather
    than containing it, so those messages keep their own canonical" is
    now false in all three of its parts: nothing is linked-not-inlined,
    nothing is dropped at any cap, and a message past the cap
    canonicalises to the PAGE that holds it. The live gate is
    `thread_is_materialised`, not a position-versus-cap comparison.

    Why the current code violates it: `README.md` and `CHANGELOG.md`
    were swept, but `mimir/web/routes/thread.py`'s module docstring and
    the comment directly above the consolidation block in
    `mimir/web/routes/message.py` still state the removed rule. Those
    are the two files a reader opens to understand the surface, and this
    project's own record (MEMORY.md 2026-07-30 and 2026-08-01) is that a
    confident comment asserting a dead invariant is precisely what lets
    the next defect survive re-reading -- three such comments on this
    branch alone, one of which had already propagated into the operator
    docs.

    Asserted as a documentation-currency guard rather than a behaviour
    guard because the BEHAVIOUR is already correct and pinned above; it
    is only the prose that disagrees with it. The behavioural half is
    re-asserted here too so the test cannot pass by the comments being
    right about a regression.
    """
    import pathlib

    from mimir.config import settings

    # The behaviour the comments deny: a third message at cap 2 is
    # rendered (on page 2) and canonicalises there, rather than being
    # "listed as a link" and keeping its own canonical.
    settings_cap = settings.thread_view_render_cap
    try:
        settings.thread_view_render_cap = 2
        seeded = _seed_three_message_thread(tmp_path, "alpha")
        _, root_url, _ = seeded["root"]
        _, nested_url, _ = seeded["nested"]
        nested_id = int(nested_url.rsplit("/", 1)[1])
        page2 = client.get(root_url + "/t/2").get_data(as_text=True)
        assert f'id="m{nested_id}"' in page2, (
            "premise stale: the third message is no longer rendered on "
            "page 2, so the comments under test may have become true again"
        )
    finally:
        settings.thread_view_render_cap = settings_cap

    root = pathlib.Path(__file__).resolve().parent.parent.parent / "mimir"
    # Phrases unique to the removed rule. Matched case-insensitively on
    # collapsed whitespace so a re-wrap cannot smuggle one past.
    #
    # This matches TEXT, not meaning, so a sentence using one of these
    # phrases about a different subject trips it too: the unrankable
    # case genuinely does keep a per-message canonical, and saying so in
    # those exact words fails here. That is the intended direction. A
    # doc-drift guard should fail closed and be answered by rewording
    # the comment, not by narrowing the list, because every phrase
    # removed from it is a claim that can quietly come back.
    dead_claims = [
        "the tail is linked rather than inlined",
        "those messages keep their own canonical",
        "the thread view only links to a message rather than containing it",
        "those messages keep their own self-canonical",
    ]
    offenders = []
    for path in (
        root / "web" / "routes" / "thread.py",
        root / "web" / "routes" / "message.py",
    ):
        text = " ".join(path.read_text().split()).lower()
        # Comment markers are stripped by the whitespace collapse above
        # only for `#`-prefixed lines, so normalise those away too.
        text = text.replace("# ", "")
        for claim in dead_claims:
            if claim in text:
                offenders.append(f"{path.name}: {claim!r}")
    assert not offenders, (
        "these source files still assert the containment rule 3.8.0 "
        "removed; the README and CHANGELOG were updated for the same "
        "change and these were not:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("variant", ["pages", "hx"])
def test_thread_pages_share_one_pill_compute(client, tmp_path, monkeypatch, variant):
    """The pill and card caches exist because the pill walks the whole
    thread (about 120 ms on the largest one). Keyed by the full ETag,
    every page of a thread and both representations missed separately, so
    a crawler walking a long thread's pages paid the walk on each. They
    key by the ETag's content part, which every page of one thread shares."""
    from mimir import lifecycle_status
    from mimir.config import settings
    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    monkeypatch.setattr(settings, "thread_view_render_cap", 1)
    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [("root@x", None), ("r1@x", "root@x"), ("r2@x", "root@x")],
    )
    root_id, root_url = seeded["root@x"]
    with SessionLocal() as s:
        s.get(Article, root_id).subject = "[PATCH] foo: bar"
        s.commit()
    calls = []
    real = lifecycle_status._bulk_uncached

    def counting(session, ids, inbox_id):
        calls.append(list(ids))
        return real(session, ids, inbox_id)

    monkeypatch.setattr(lifecycle_status, "_bulk_uncached", counting)
    if variant == "pages":
        urls = [(root_url + "/t", {}), (root_url + "/t/2", {}), (root_url + "/t/3", {})]
    else:
        urls = [(root_url + "/t", {}), (root_url + "/t", {"HX-Request": "true"})]
    for url, headers in urls:
        assert client.get(url, headers=headers).status_code == 200, url
    walks = [c for c in calls if c == [root_id]]
    assert walks, "precondition: the pill rendered at all"
    assert len(walks) == 1, f"{variant}: walk ran {len(walks)} times for {len(urls)}"


def _set_versions(seeded, versions):
    from mimir.extensions import SessionLocal

    with SessionLocal() as s:
        for mid, version in versions.items():
            a = s.get(Article, seeded[mid][0])
            a.patch_series_key = "serieskey"
            a.patch_series_version = version
            a.patch_series_position = 0
        s.commit()


def _get(client, url):
    from sqlalchemy import delete

    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    with SessionLocal() as s:
        s.execute(delete(CacheEntry))
        s.commit()
    r = client.get(url)
    assert r.status_code == 200
    return r


@pytest.mark.parametrize("surface", ["message", "thread"])
def test_newest_revision_renumbered_in_place_moves_the_etag(client, tmp_path, surface):
    """Root v1 is superseded by v9 and v10; a reprocess renumbers v10 to
    v11. Count stays 3 and text max stays "v9"; the tooltip changes."""
    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [("root@x", None), ("nine@x", "root@x"), ("ten@x", "root@x")],
    )
    root_id, root_url = seeded["root@x"]
    url = root_url if surface == "message" else root_url + "/t"
    with SessionLocal() as s:
        s.get(Article, root_id).subject = "[PATCH] foo: bar"
        s.commit()
    _set_versions(seeded, {"root@x": "v1", "nine@x": "v9", "ten@x": "v10"})
    before = _get(client, url)
    assert re.search(r"Superseded by v10 posted", before.get_data(as_text=True))

    _set_versions(seeded, {"ten@x": "v11"})
    after = _get(client, url)
    # Precondition: the change is visible in the body.
    assert re.search(r"Superseded by v11 posted", after.get_data(as_text=True))
    assert before.headers["ETag"] != after.headers["ETag"], (
        f"{surface}: tooltip moved v10 -> v11 under an unchanged ETag"
    )


@pytest.mark.parametrize("surface", ["message", "thread"])
def test_superseded_flip_in_place_moves_the_etag(client, tmp_path, surface):
    """Root v10 with a sibling v2 is current. A reprocess renumbers the
    root to v1, so v2 now supersedes it. Count stays 2 and text max of
    {"v10", "v2"} and {"v1", "v2"} is "v2" both times; the pill flips."""
    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("root@x", None), ("other@x", "root@x")]
    )
    root_id, root_url = seeded["root@x"]
    url = root_url if surface == "message" else root_url + "/t"
    with SessionLocal() as s:
        s.get(Article, root_id).subject = "[PATCH] foo: bar"
        s.commit()
    _set_versions(seeded, {"root@x": "v10", "other@x": "v2"})
    before = _get(client, url)
    assert not re.search(r"\bSUPERSEDED\b", before.get_data(as_text=True))

    _set_versions(seeded, {"root@x": "v1"})
    after = _get(client, url)
    assert re.search(r"\bSUPERSEDED\b", after.get_data(as_text=True))
    assert before.headers["ETag"] != after.headers["ETag"], (
        f"{surface}: pill flipped to SUPERSEDED under an unchanged ETag"
    )


@pytest.mark.parametrize("v1_position", [None, 0])
def test_fold_diff_link_resolves_for_a_null_position_cover(
    client, tmp_path, v1_position
):
    """Every [diff against previous] link the revisions fold emits must
    resolve. The fold treats a NULL position as 0, so the diff route
    must too."""
    import html as htmllib

    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(tmp_path, "alpha", [("v1@x", None), ("v2@x", None)])
    with SessionLocal() as s:
        for mid, version, position in (("v1@x", "v1", v1_position), ("v2@x", "v2", 0)):
            a = s.get(Article, seeded[mid][0])
            a.subject = f"[PATCH {version} 0/1] foo: bar"
            a.patch_series_key = "nullposkey"
            a.patch_series_version = version
            a.patch_series_position = position
        s.commit()
    page = client.get(seeded["v1@x"][1]).get_data(as_text=True)
    links = re.findall(r'href="([^"]*/series/[^"]*)"', page)
    assert len(links) == 1, links  # precondition: the fold links v2
    assert client.get(htmllib.unescape(links[0])).status_code == 200


def test_thread_view_links_the_series_diff_through_the_revisions_fold(client, tmp_path):
    """#846: a message page in a multi-message thread canonicalises to
    `/t`, and the fold that links the series diffs was only on the
    message page, so crawlers that follow the canonical never saw a diff
    link. `/t` already computes the root's patch state; it renders the
    same fold, and its link resolves."""
    import html as htmllib

    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("v1@x", None), ("v2@x", None), ("re@x", "v2@x")]
    )
    with SessionLocal() as s:
        for mid, version in (("v1@x", "v1"), ("v2@x", "v2")):
            a = s.get(Article, seeded[mid][0])
            a.subject = f"[PATCH {version} 0/1] foo: bar"
            a.patch_series_key = "tfoldkey"
            a.patch_series_version = version
            a.patch_series_position = 0
        s.commit()
    page = client.get(seeded["v2@x"][1] + "/t")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'class="revisions-fold"' in html
    links = re.findall(r'href="([^"]*/series/[^"]*/diff\?[^"]*)"', html)
    assert len(links) == 1, links
    link = htmllib.unescape(links[0])
    assert "from=v1&to=v2&pos=cover" in link
    assert client.get(link).status_code == 200


def _seed_cover_series(tmp_path, key, versions):
    """One cover letter per entry of `versions`, posted a day apart in
    list order, plus a reply to the last so its `/t` is a real thread.
    Returns `seed_thread_shape`'s map; message ids are `c<i>@x`."""
    from mimir.extensions import SessionLocal
    from tests.test_routes._helpers import seed_thread_shape

    mids = [f"c{i}@x" for i in range(len(versions))]
    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [(m, None) for m in mids] + [("reply@x", mids[-1])],
        date_for={
            m: f"Mon, {i + 1} Jan 2024 00:00:00 +0000" for i, m in enumerate(mids)
        },
    )
    with SessionLocal() as s:
        for mid, version in zip(mids, versions, strict=True):
            a = s.get(Article, seeded[mid][0])
            a.subject = f"[PATCH {version or ''} 0/1] foo: bar"
            a.patch_series_key = key
            a.patch_series_version = version
            a.patch_series_position = 0
        s.commit()
    return seeded


def _fold(html):
    """The revisions fold's markup, so links elsewhere cannot count."""
    return html.split('class="revisions-fold"')[1].split("</details>")[0]


def test_fold_diff_links_are_the_sitemap_s_urls_byte_for_byte(client, tmp_path):
    """#844: the fold links each revision's diff against the previous
    one, which is exactly the set of URLs the series diff sitemap
    advertises (#846). Both come from `adjacent_versions`, and a link
    and a sitemap entry must be the same bytes, not equivalent ones."""
    import html as htmllib

    from tests.test_routes._helpers import _clear_sitemap_cache

    key = "b" * 40
    seeded = _seed_cover_series(tmp_path, key, ["v1", "v2", "v3"])
    page = client.get(seeded["c2@x"][1] + "/t").get_data(as_text=True)
    links = [
        htmllib.unescape(h)
        for h in re.findall(r'href="([^"]*/series/[^"]*)"', _fold(page))
    ]

    _clear_sitemap_cache()
    xml = client.get("/sitemap-series-diffs-b.xml").get_data(as_text=True)
    # Absolute in the sitemap, site-relative on the page: compare what
    # follows the origin, which both build through `_spell_series_diff_url`.
    locs = [htmllib.unescape(loc) for loc in re.findall(r"<loc>([^<]+)</loc>", xml)]
    sitemap_paths = [
        re.sub(r"^https?://[^/]+", "", loc) for loc in locs if f"/series/{key}/" in loc
    ]

    assert len(sitemap_paths) == 2, sitemap_paths  # precondition: v1-v2, v2-v3
    assert sorted(links) == sorted(sitemap_paths)


def test_fold_links_each_revision_to_its_previous_and_a_resend_to_nothing(
    client, tmp_path
):
    """v1 has no previous revision, so no link. A bare "resend" and a
    NULL version are not revisions the route can diff, so they neither
    get a link nor count as the previous one. A second v2 is a resend of
    the first and is not a step: it is no entry of its own (#861) but is
    listed under the first v2, which links v1 to v2. v3 links v2 to v3. Every emitted link is served. The sitemap's query
    drops the bare and NULL versions itself, so only this test holds
    `adjacent_versions` to dropping them."""
    import html as htmllib
    from urllib.parse import parse_qs, urlparse

    versions = ["v1", "resend", None, "v2", "v2", "v3"]
    seeded = _seed_cover_series(tmp_path, "c" * 40, versions)
    fold = _fold(client.get(seeded["c5@x"][1] + "/t").get_data(as_text=True))
    rows = re.findall(r"<li[^>]*>(.*?)</li>", fold, re.DOTALL)
    # Precondition: one row per version, the second v2 listed under the first.
    assert len(rows) == len(set(versions)), rows
    assert fold.count("re-posted") == 1, fold

    pairs = []
    for row in rows:
        hrefs = re.findall(r'href="([^"]*/series/[^"]*)"', row)
        assert len(hrefs) <= 1, row
        if not hrefs:
            pairs.append(None)
            continue
        link = htmllib.unescape(hrefs[0])
        q = parse_qs(urlparse(link).query)
        pairs.append((q["from"][0], q["to"][0]))
        assert client.get(link).status_code == 200, link
    assert pairs == [None, None, None, ("v1", "v2"), ("v2", "v3")]


def test_the_thread_view_shows_the_revisions_open_and_the_message_page_folded(
    client, tmp_path
):
    """#844: on `/t` the revisions are visible without a click, since the
    series diff is the most original page mimir serves. The message page
    keeps its fold closed."""
    seeded = _seed_cover_series(tmp_path, "d" * 40, ["v1", "v2"])
    url = seeded["c1@x"][1]

    def fold_tag(html):
        tags = re.findall(r'<details class="revisions-fold"[^>]*>', html)
        assert len(tags) == 1, tags
        return tags[0]

    assert re.search(
        r"\sopen\b", fold_tag(client.get(url + "/t").get_data(as_text=True))
    )
    assert not re.search(r"\sopen\b", fold_tag(client.get(url).get_data(as_text=True)))


# Statements one `/t` request runs for a two-revision cover with one
# reply, counted 2026-10-25 on develop e36429e before #844: a full
# render with caches cleared, and the 304 that revalidates it. The
# revisions block is a render of data the page already reads, so it
# must add none (#844 budget: +0 for both). 32 since #844 Part 3: the
# status line reads the lifecycle row the pill already fetched, and the
# card's own landing query is gone. #989 adds one statement before the
# validator (the thread's series rows, filtered in SQL, for the digest)
# and, on a 200 only, the whole thread's rows (`thread_rows`) for the
# tabs and the Patches index: 34/16. The header lists this inbox's busiest
# subsystem first, a cached ranking read before the validator (it is an
# ETag input): one more statement on both, 35/17.
# The outline (#989, from five messages) adds no statement on page 1, which
# reads its rows with its own slice, and one on pages 2 and up, on the 200 and
# the 304, pinned in test_thread_outline.py. This two-message thread has no
# outline and one page.
# Revision links on `/t` name the thread page holding each revision: one
# statement for the revisions' links, two for `unmaterialised_roots` per
# inbox, and one `thread_rows` per other thread holding a revision below
# its root. Here the v1 cover is its own root (page 1, no read), so three
# more on the 200 only: 38/17.
THREAD_VIEW_STATEMENTS_200 = 38
THREAD_VIEW_STATEMENTS_304 = 17


def test_thread_view_statement_budget(client, tmp_path):
    from sqlalchemy import event

    from mimir.extensions import engine

    seeded = _seed_cover_series(tmp_path, "e" * 40, ["v1", "v2"])
    url = seeded["c1@x"][1] + "/t"
    statements: list[str] = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", grab)
    try:
        full = _get(client, url)
        n_full = len(statements)
        statements.clear()
        revalidated = client.get(url, headers={"If-None-Match": full.headers["ETag"]})
        n_304 = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", grab)

    assert full.status_code == 200
    assert revalidated.status_code == 304
    assert (n_full, n_304) == (
        THREAD_VIEW_STATEMENTS_200,
        THREAD_VIEW_STATEMENTS_304,
    )


def test_thread_view_renders_a_genuine_single_message_thread(client, tmp_path):
    """#1003: a single-message thread's `/t` is its indexed page. A plain
    singleton, not the self-cycle above: one message, self-canonical, no
    `noindex`, no reply count in the structured data (there are no
    replies), and the whole body rather than the first
    `BODY_TEXT_FOLD_LINES` lines behind a "more" link, which exists to keep
    long threads light."""
    from mimir.rendering.body import BODY_TEXT_FOLD_LINES

    lines = [f"plain line {i}" for i in range(BODY_TEXT_FOLD_LINES + 20)]
    _, url = _ingest_one_article(
        tmp_path, "alpha", "genuine-solo@x", body="\n".join(lines).encode()
    )
    r = client.get(url + "/t")
    html = r.get_data(as_text=True)
    posting = next(
        g
        for g in _json_ld_blocks(html)[0]["@graph"]
        if g["@type"] == "DiscussionForumPosting"
    )
    assert {
        "status": r.status_code,
        "canonical": _canonical_of(html),
        "noindex": 'name="robots"' in html,
        "messages": html.count('class="thread-message"'),
        "interactionStatistic": "interactionStatistic" in posting,
        "folded": "text-more" in html,
        # In the rendered body, not just anywhere: the structured data and
        # the meta description carry body text too.
        "last line in body": lines[-1]
        in html[html.index('class="message-body"') : html.index("</article>")],
    } == {
        "status": 200,
        "canonical": f"http://localhost{url}/t",
        "noindex": False,
        "messages": 1,
        "interactionStatistic": False,
        "folded": False,
        "last line in body": True,
    }
