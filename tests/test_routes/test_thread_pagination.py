"""The whole-thread view's pagination contract, over the full axis matrix.

Written before the implementation, and deliberately as ONE invariant
asserted over a cross-product rather than a family of narrow tests. The
three adversarial rounds that preceded this file each found a defect
that a narrow test could not see, and in every case the fixture had held
an axis fixed: one inbox, one instant, one author, a size that was an
exact multiple of the cap, or only the materialised path.

The invariant, stated once:

    Every message in a thread is rendered on exactly one page; the pages
    together are exactly the thread; and whatever a message claims as its
    canonical is a URL that renders it.

Everything else here is a corollary of that or a property the invariant
cannot express (counts, ordering, structured data).
"""

import re

import pytest

from tests.test_routes._helpers import (
    _seed_three_message_thread,
    build_thread,
)


def _canonical(html):
    m = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    return m.group(1) if m else None


def _rendered(html):
    return [int(m) for m in re.findall(r'class="thread-message" id="m(\d+)"', html)]


def _walk_pages(client, root_url):
    """Follow the real next-link, as a reader or crawler would.

    Not by constructing `/t/2`, `/t/3`: a page that exists but cannot be
    reached by following links is unreachable in the sense that matters,
    and this project has shipped that before.
    """
    pages, url, guard = {}, root_url + "/t", 0
    while url is not None:
        guard += 1
        assert guard < 100, f"pagination did not terminate at {url}"
        html = client.get(url).get_data(as_text=True)
        assert "<h1" in html, f"{url} did not render a page"
        for art_id in _rendered(html):
            assert art_id not in pages, f"{art_id} rendered on two pages"
            pages[art_id] = url
        nxt = re.search(r'<div class="thread-more">\s*<a href="([^"]+)"', html)
        url = nxt.group(1) if nxt else None
    return pages


# The axes. Kept as an explicit cross-product so adding a value to one
# axis re-tests every combination of the others, which is the property
# the previous round's hand-written fixtures lacked.
@pytest.mark.parametrize("shape", ["chain", "fan_out", "bushy", "off_list"])
@pytest.mark.parametrize(
    "size,cap",
    [
        (6, 50),  # single page
        (6, 6),  # exactly one page: the boundary an off-by-one lands on
        (6, 3),  # exact multiple: floor == ceil, hides ceiling bugs
        (7, 3),  # NOT a multiple: the case that catches them
        (6, 1),  # one message per page
    ],
)
@pytest.mark.parametrize("unroot", [(), (0,), (1,), (3,)])
def test_pages_partition_the_thread_and_canonicals_point_at_them(
    client, tmp_path, monkeypatch, shape, size, cap, unroot
):
    """The invariant, across shape x size-vs-cap x which rows are rooted.

    `unroot` is the axis that mattered most and was hardest to see:

      ()    every row materialised, the fast path.
      (0,)  the ROOT is unrooted. Nothing in the thread is rooted, so any
            check that detects unrooted rows by looking at their PARENTS
            finds nothing and wrongly reports the thread healthy.
      (1,)  an early member: the resulting rank shift crosses a page
            boundary and misdirects every message after it.
      (3,)  a later member: the shift does NOT cross a boundary at small
            caps, which is why a fixture that only nulled here passed
            while the bug was live.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", cap)
    seeded = build_thread(tmp_path, "alpha", shape=shape, size=size, unroot=unroot)
    ids = {aid for aid, _ in seeded.values()}
    _root_id, root_url = seeded["m0"]

    pages = _walk_pages(client, root_url)
    assert set(pages) == ids, (
        f"pages do not partition the thread; missing={ids - set(pages)} "
        f"extra={set(pages) - ids}"
    )

    for art_id, page_url in pages.items():
        _, msg_url = next(v for v in seeded.values() if v[0] == art_id)
        canonical = _canonical(client.get(msg_url).get_data(as_text=True))
        assert canonical is not None, f"{msg_url} emitted no canonical"
        if canonical.endswith(msg_url):
            continue  # self-canonical: claims nothing about a page
        assert canonical.endswith(page_url), (
            f"article {art_id} renders on {page_url} but canonicalises to {canonical}"
        )
        # A page canonical is a containment CLAIM, so verify it.
        target = client.get(canonical.replace("http://localhost", ""))
        assert art_id in _rendered(target.get_data(as_text=True)), (
            f"article {art_id} canonicalises to {canonical}, which does not render it"
        )


def test_consolidation_actually_happens_when_the_thread_is_healthy(
    client, tmp_path, monkeypatch
):
    """The positive half, pinned separately.

    The invariant test above accepts a self-canonical message, because
    that is legitimate when the column cannot rank the thread. That
    escape hatch made an earlier version of it VACUOUS: nulling any
    member made every message self-canonicalise, so the containment
    branch never executed in any parametrisation and an implementation
    that never consolidated at all would have passed.

    So: on a healthy thread, consolidation must happen.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=5)
    _root_id, root_url = seeded["m0"]

    pages = _walk_pages(client, root_url)
    assert len(set(pages.values())) == 3, "fixture did not paginate"

    consolidated = 0
    for art_id, page_url in pages.items():
        _, msg_url = next(v for v in seeded.values() if v[0] == art_id)
        canonical = _canonical(client.get(msg_url).get_data(as_text=True))
        if canonical and not canonical.endswith(msg_url):
            assert canonical.endswith(page_url)
            consolidated += 1
    assert consolidated == len(pages), (
        f"only {consolidated} of {len(pages)} messages consolidated onto "
        "their page; the thread is healthy so all of them should"
    )


def test_thread_membership_is_scoped_to_one_inbox(client, tmp_path, monkeypatch):
    """A cross-posted thread must not merge the two inboxes' copies.

    No fixture in the previous round cross-posted WITH `thread_root_id`
    populated in both inboxes, so dropping the `inbox_id` filter from the
    membership query, the count and the rank were all invisible: three
    separate mutants, all surviving the whole suite.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 50)
    seeded = build_thread(
        tmp_path, "alpha", shape="chain", size=4, cross_post_to="beta"
    )
    _root_id, root_url = seeded["m0"]

    html = client.get(root_url + "/t").get_data(as_text=True)
    assert len(_rendered(html)) == 4, (
        f"rendered {len(_rendered(html))} messages for a 4-message thread; "
        "the other inbox's rows leaked in"
    )
    assert "4 messages" in html


def test_last_activity_is_the_newest_message_not_the_oldest(
    client, tmp_path, monkeypatch
):
    """Dates must be spread for this to mean anything.

    Every fixture stamped messages seconds apart, so `max()` and `min()`
    over the thread's dates rendered the same relative-time string and a
    mutant swapping them survived.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 50)
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=4, dates="spread")
    _root_id, root_url = seeded["m0"]

    from mimir.extensions import SessionLocal
    from mimir.models import Article

    with SessionLocal() as s:
        dates = [s.get(Article, aid).date for aid, _ in seeded.values()]
    assert max(dates) - min(dates) > __import__("datetime").timedelta(days=60), (
        "fixture did not spread the dates; the assertion below is vacuous"
    )

    from mimir.web.filters import _relative_time

    html = client.get(root_url + "/t").get_data(as_text=True)
    # Scoped to the summary line. Asserting against the whole page is a
    # substring collision waiting to happen: `_relative_time` renders an
    # ISO date for old messages, and every message's own `Date:` header
    # contains its date, so the OLDEST date is present on the page no
    # matter which one the summary reports.
    summary = re.search(r"message[s]?,\s*\d+ author[s]?,\s*([^<\n&]+)", html)
    assert summary, f"no summary line in the page: {html[:400]}"
    reported = summary.group(1).strip()
    assert reported == _relative_time(max(dates)), (
        f"summary reports last activity as {reported!r}, expected "
        f"{_relative_time(max(dates))!r} (oldest is "
        f"{_relative_time(min(dates))!r})"
    )


def test_author_count_is_by_address_not_by_display_name(client, tmp_path, monkeypatch):
    """Display-name drift must not fragment the tally.

    Every fixture used one author with one spelling, so a tally over raw
    strings and a tally over parsed addresses agreed and a mutant could
    not be seen.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 50)
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=6, authors=2)
    _root_id, root_url = seeded["m0"]

    html = client.get(root_url + "/t").get_data(as_text=True)
    assert "2 authors" in html, (
        "six messages from two addresses under six different display "
        "names must count as two people"
    )


@pytest.mark.parametrize("shape", ["chain", "fan_out", "bushy"])
def test_both_membership_paths_render_the_same_order(
    client, tmp_path, monkeypatch, shape
):
    """The two paths must agree on ORDER, not just on membership.

    Everything else here compares sets, so the fallback's sort is
    invisible to it: deleting the line that puts the walk's output into
    page order changes nothing any set-comparison can see, while the
    rank used to build canonicals keeps using chronological order. That
    divergence is what put messages on pages their canonical did not
    name, twice.

    Asserted by rendering the SAME thread through both paths and
    diffing the sequence. Unrooting a member does not change membership
    (the walk finds everything), so any difference is ordering.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)

    (tmp_path / "fast").mkdir()
    (tmp_path / "slow").mkdir()
    fast = build_thread(tmp_path / "fast", "alpha", shape=shape, size=7)
    _fid, fast_url = fast["m0"]
    fast_order = [
        art_id
        for page in dict.fromkeys(_walk_pages(client, fast_url).values())
        for art_id in _rendered(client.get(page).get_data(as_text=True))
    ]

    slow = build_thread(tmp_path / "slow", "beta", shape=shape, size=7, unroot=(3,))
    _sid, slow_url = slow["m0"]
    slow_order = [
        art_id
        for page in dict.fromkeys(_walk_pages(client, slow_url).values())
        for art_id in _rendered(client.get(page).get_data(as_text=True))
    ]

    # Same shape, same arrival order, so the two sequences must be the
    # same positions even though the article ids differ between inboxes.
    fast_pos = {a: i for i, a in enumerate(fast_order)}
    slow_pos = {a: i for i, a in enumerate(slow_order)}
    fast_by_mid = {mid: fast_pos[aid] for mid, (aid, _) in fast.items()}
    slow_by_mid = {mid: slow_pos[aid] for mid, (aid, _) in slow.items()}
    assert fast_by_mid == slow_by_mid, (
        "the materialised path and the walk render the thread in "
        f"different orders:\n  fast={fast_by_mid}\n  walk={slow_by_mid}"
    )


@pytest.mark.parametrize("dates", ["dense", "spread", "dateless", "ties"])
def test_pages_partition_the_thread_across_date_shapes(
    client, tmp_path, monkeypatch, dates
):
    """The date axis, crossed with pagination rather than tested alone.

    `ties` is the one that had no fixture anywhere: `seed_thread_shape`
    stamps arrival as `commit_time + i`, so date order and id order
    agreed in every case and the `id` tie-break in both the SQL ORDER BY
    and the Python sort key was unreachable. Two replies sharing an
    arrival second is ordinary (a mail burst committed in the same
    second), and without the tie-break the page a message renders on and
    the page its canonical names can differ.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    size = 7
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=size, dates=dates)
    ids = {aid for aid, _ in seeded.values()}
    _root_id, root_url = seeded["m0"]

    pages = _walk_pages(client, root_url)
    assert set(pages) == ids, f"{dates}: pages do not partition the thread"

    asserted = 0
    for art_id, page_url in pages.items():
        _, msg_url = next(v for v in seeded.values() if v[0] == art_id)
        canonical = _canonical(client.get(msg_url).get_data(as_text=True))
        if canonical is None or canonical.endswith(msg_url):
            continue
        asserted += 1
        assert canonical.endswith(page_url), (
            f"{dates}: article {art_id} renders on {page_url} but "
            f"canonicalises to {canonical}"
        )

    # How many messages actually reached the assertion, pinned. The
    # `continue` above is legitimate (a self-canonical message claims
    # nothing about a page), but it also silently absorbs a fixture that
    # has stopped producing resolvable URLs: `spread` ran 2 of 7 for
    # exactly that reason and looked like full coverage. A count is the
    # cheapest thing that fails when an axis quietly stops testing.
    # Per-axis, because `dateless` legitimately cannot reach it for its
    # dateless members: the URL carries the date, so those message pages
    # 404 by design and have no canonical to compare. Every other
    # message must.
    from tests.test_routes._helpers import _dateless_indices

    unreachable = len(_dateless_indices(size)) if dates == "dateless" else 0
    assert asserted == size - unreachable, (
        f"{dates}: only {asserted} of {size - unreachable} reachable "
        "messages hit the containment assertion; the axis has stopped "
        "exercising it"
    )


def test_cross_posted_thread_ranks_pages_per_inbox(client, tmp_path, monkeypatch):
    """The rank must be inbox-scoped too, not just membership and count.

    The existing cross-post test ran at cap 50 on a 4-message thread, so
    `thread_page_of` returned 1 unconditionally and its `inbox_id` filter
    was never exercised: deleting that filter passed the whole suite
    while messages canonicalised to pages that 404.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = build_thread(
        tmp_path, "alpha", shape="chain", size=6, cross_post_to="beta"
    )
    _root_id, root_url = seeded["m0"]

    pages = _walk_pages(client, root_url)
    assert len(set(pages.values())) == 3, (
        f"6 messages at cap 2 should be 3 pages, got {set(pages.values())}"
    )
    for art_id, page_url in pages.items():
        _, msg_url = next(v for v in seeded.values() if v[0] == art_id)
        canonical = _canonical(client.get(msg_url).get_data(as_text=True))
        assert canonical is not None
        target = canonical.replace("http://localhost", "")
        assert client.get(target).status_code == 200, (
            f"article {art_id} canonicalises to {target}, which 404s"
        )
        assert art_id in _rendered(client.get(target).get_data(as_text=True))


def test_a_reply_reaches_the_page_that_holds_it(client, tmp_path, monkeypatch):
    """`/t` on a reply must land on the reply's page, in ONE hop.

    It redirected to page 1 and dropped the page, so the app
    simultaneously told crawlers "this message lives on page 3" (its
    canonical) and permanently redirected its thread request to page 1.
    `/t/<n>` on a reply did the same, laundering an out-of-range page
    into a 200.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=7)
    _root_id, root_url = seeded["m0"]
    pages = _walk_pages(client, root_url)

    for mid in ("m4", "m5", "m6"):
        art_id, msg_url = seeded[mid]
        resp = client.get(msg_url + "/t")
        assert resp.status_code == 301, f"{mid}: expected a redirect"
        target = resp.headers["Location"].replace("http://localhost", "")
        assert target == pages[art_id], (
            f"{mid} renders on {pages[art_id]} but its /t redirects to {target}"
        )
        # One hop, not two.
        assert client.get(target).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/nosuchinbox/2024/01/1/t/1",
        "/alpha/2024/01/99999/t/1",
        "/%0d%0aX-Injected:%20y/2024/01/1/t/1",
    ],
)
def test_page_one_collapse_never_redirects_an_unvalidated_url(client, tmp_path, path):
    """The `/t/1` collapse must not run before the existence checks.

    It did, reflecting raw URL segments into a `Location` header. Every
    bogus path suffixed `/t/1` became a cacheable 301 to a 404, and a
    segment carrying an encoded CRLF raised `ValueError: Header values
    must not contain newline characters` inside `redirect()` -- a 500 on
    a public URL, with the edge forwarding those bytes rather than
    rejecting them.
    """
    _seed_three_message_thread(tmp_path, "alpha")
    resp = client.get(path)
    assert resp.status_code == 404, (
        f"{path} returned {resp.status_code}; a URL that does not resolve "
        "must 404 rather than redirect or raise"
    )


def test_pagination_furniture_is_present_when_it_should_be(
    client, tmp_path, monkeypatch
):
    """Every piece of it was asserted only as ABSENT on a single page.

    So deleting `rel=next`, `rel=prev`, the previous-page link, the
    "page X of Y" counter and the remaining-count all survived the suite:
    five mutants, none observable, because nothing ever asserted the
    positive case.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=5)
    _root_id, root_url = seeded["m0"]

    page1 = client.get(root_url + "/t").get_data(as_text=True)
    assert '<link rel="next"' in page1
    assert '<link rel="prev"' not in page1
    assert "page 1 of 3" in page1
    # 5 messages, 2 rendered on page 1, so 3 remain.
    assert "Next 2 of 3 remaining" in page1

    page2 = client.get(root_url + "/t/2").get_data(as_text=True)
    assert '<link rel="prev"' in page2
    assert '<link rel="next"' in page2
    assert "page 2 of 3" in page2
    assert "Previous page" in page2

    page3 = client.get(root_url + "/t/3").get_data(as_text=True)
    assert '<link rel="next"' not in page3, "rel=next past the last page"
    assert "page 3 of 3" in page3
    assert 'class="thread-more"' not in page3


def test_an_empty_slice_is_a_404_not_a_500(client, tmp_path, monkeypatch):
    """The count and the slice are separate queries with no isolation.

    pysqlite gives a read session no snapshot isolation across
    statements, so rows can vanish between `thread_aggregates` and the
    page query (a concurrent `reindex --from-scratch`, or the
    `break_cycle` stall). The bounds check passed on the stale count and
    the render then indexed `nodes[0]` in the JSON-LD builder: a 500 on
    a public URL, from a data state the code's own docstrings call
    reachable.

    Simulated by emptying the slice, because the race itself is not
    reproducible in a single-threaded test and the guard is what matters.
    """
    from mimir.config import settings
    from mimir.web.routes import thread as thread_route

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=5)
    _root_id, root_url = seeded["m0"]
    assert client.get(root_url + "/t").status_code == 200

    monkeypatch.setattr(thread_route, "thread_by_root_id", lambda *a, **k: [])
    resp = client.get(root_url + "/t")
    assert resp.status_code == 404, (
        f"an empty slice returned {resp.status_code}; it must 404 rather "
        "than reach the JSON-LD builder with no root"
    )


def test_a_date_tie_across_a_page_boundary_does_not_drift(
    client, tmp_path, monkeypatch
):
    """Two messages sharing an arrival second, straddling a page edge.

    `seed_thread_shape` stamps arrival as `commit_time + i`, so date
    order and id order agree in every other fixture and the `id`
    tie-break in the rank predicate is unreachable. A tie WITHIN one
    page also proves nothing, because both members land on the same page
    either way. The tie has to cross the boundary.
    """
    from sqlalchemy import select

    from mimir.config import settings
    from mimir.extensions import SessionLocal
    from mimir.models import Article

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    # The tie comes from the fixture's own `ties` axis rather than a
    # second hand-rolled UPDATE here. Two places creating ties is two
    # places to drift, and the drift is invisible: this test would still
    # pass with the shared axis tying the wrong pair, which is how the
    # axis came to guard nothing at all.
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=6, dates="ties")
    _root_id, root_url = seeded["m0"]
    ids = [seeded[f"m{i}"][0] for i in range(6)]

    tied = (ids[1], ids[2])

    # The fixture's PRECONDITION, asserted rather than assumed. Without
    # this the test passes when the `ties` axis stops producing a tie:
    # the pair still straddles the boundary and every canonical is still
    # right, because a thread with no tie has nothing to get wrong. It
    # would read as green while exercising the untied case, which every
    # other test already covers.
    with SessionLocal() as s:
        dates = [
            s.execute(select(Article.date).where(Article.id == i)).scalar_one()
            for i in tied
        ]
    assert dates[0] == dates[1], (
        f"the fixture did not tie {tied}: {dates}. The `ties` axis has "
        "stopped producing the condition this test exists for"
    )

    pages = _walk_pages(client, root_url)
    assert pages[tied[0]] != pages[tied[1]], (
        "the TIED pair did not straddle a boundary; the assertion below "
        "would hold whether or not the tie-break exists"
    )
    for art_id in tied:
        _, msg_url = next(v for v in seeded.values() if v[0] == art_id)
        canonical = _canonical(client.get(msg_url).get_data(as_text=True))
        assert canonical is not None and canonical.endswith(pages[art_id]), (
            f"tied article {art_id} renders on {pages[art_id]} but "
            f"canonicalises to {canonical}"
        )


def test_the_last_next_link_promises_what_the_last_page_delivers(
    client, tmp_path, monkeypatch
):
    """The label must be `min(remaining, page_size)`, not `page_size`.

    On every page but the last those two are equal, so a fixture that
    only reads page 1 cannot tell them apart. On the last hop the
    remainder is smaller than a full page and the control would promise
    more messages than the next page contains.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=5)
    _root_id, root_url = seeded["m0"]

    page2 = client.get(root_url + "/t/2").get_data(as_text=True)
    assert "Next 1 of 1 remaining" in page2, (
        "the last next-link should promise the single message page 3 "
        f"actually holds; page 2 said: "
        f"{re.search(r'Next [^<]*', page2).group(0) if 'Next ' in page2 else 'nothing'}"
    )
    assert len(_rendered(client.get(root_url + "/t/3").get_data(as_text=True))) == 1


@pytest.mark.parametrize("size", [4, 5, 6, 7, 8])
def test_dateless_fixture_builds_what_it_promises(size):
    """The fixture axis itself, pinned.

    `_dateless_indices` promises "two NON-ADJACENT replies, never the
    root". Its first version computed `(2, size - 2)`, which collides
    into a single index at size 4 and is adjacent at size 5, so at those
    sizes it built neither the count nor the separation it claimed. No
    test used the axis, so nothing noticed, and a test that later did
    would have been quietly weaker than its docstring.
    """
    from tests.test_routes._helpers import _dateless_indices

    idx = _dateless_indices(size)
    assert 0 not in idx, f"size {size}: nulled the root"
    assert len(idx) == len(set(idx)), f"size {size}: duplicate index {idx}"
    assert all(i < size for i in idx), f"size {size}: index out of range {idx}"
    if size >= 5:
        assert len(idx) == 2, f"size {size}: expected two indices, got {idx}"
        assert idx[1] - idx[0] > 1, f"size {size}: indices are adjacent {idx}"


def test_a_non_canonical_page_number_does_not_self_nominate(
    client, tmp_path, monkeypatch
):
    """`/t/02` renders page 2 and must advertise `/t/2`.

    Werkzeug's `int` converter matches `\\d+`, so a leading zero is
    accepted and every zero-padding is a distinct URL for one page. No
    emitter in the codebase produces them (the link, the sitemap entry
    and the canonical all come from `_thread_view_url`), so the only way
    to arrive is by hand or by a mangling intermediary, and adding a
    redirect would be code for a caller that does not exist.

    What must hold is the weaker, load-bearing property: the page does
    not nominate ITSELF under the padded spelling. A self-canonical
    there would turn every padding into an indexable duplicate of the
    same content, which is the exact signal the canonical exists to
    suppress.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=6)
    _root_id, root_url = seeded["m0"]

    padded = client.get(f"{root_url}/t/02")
    assert padded.status_code == 200, "leading zeros stopped resolving"
    body = padded.get_data(as_text=True)
    plain = client.get(f"{root_url}/t/2").get_data(as_text=True)
    assert _rendered(body) == _rendered(plain), "/t/02 is not page 2"

    canonical = _canonical(body)
    assert canonical is not None
    assert canonical.endswith(f"{root_url}/t/2"), (
        f"padded page self-nominated as {canonical}"
    )


def test_a_reply_in_an_unrankable_thread_redirects_to_the_bare_thread_url(
    client, tmp_path, monkeypatch
):
    """`/t` on a reply names a page only when a page can be named.

    The redirect computes its page with `thread_page_of`, which ranks
    over the materialised column, while an unrankable thread RENDERS
    from the recursive walk. The column is missing rows there by
    definition, so the rank undercounts and the redirect lands on a page
    that does not hold the reply.

    It never 404s, which is what makes it silent: the walk sees at least
    as many messages as the column, so the undercounted page is always
    in range. The route logs that the thread's pages are not advertised
    on the very response that just named one.

    Note the bare `/t` is page 1, not the whole conversation: unrankable
    threads paginate like any other, they just make no page CLAIMS.

    This is the gate that shipped with round 4's fix and had nothing
    holding it: replacing it with `if True` passed 556 tests.
    """
    from mimir.config import settings

    monkeypatch.setattr(settings, "thread_view_render_cap", 2)
    # unroot=(3,) leaves the thread unrankable while keeping the root
    # itself self-rooted, so the reply still resolves to a root and the
    # redirect is reached at all.
    seeded = build_thread(tmp_path, "alpha", shape="chain", size=7, unroot=(3,))
    _root_id, root_url = seeded["m0"]
    _reply_id, reply_url = seeded["m6"]

    r = client.get(reply_url + "/t")
    assert r.status_code == 301
    location = r.headers["Location"]
    assert location.endswith(root_url + "/t"), (
        f"named page {location} for a thread whose pages cannot be ranked"
    )

    # The bare `/t` does NOT render the reply, and that is correct: an
    # unrankable thread is still paginated, it simply makes no page
    # CLAIMS. So the redirect is a navigational entry point to the
    # conversation, not a containment claim, and the reply keeps its own
    # canonical. Asserting containment here (the first version of this
    # test) fails against correct behaviour.
    landed_path = location.replace("http://localhost", "")
    assert _reply_id not in _rendered(client.get(landed_path).get_data(as_text=True))
    assert _reply_id in _walk_pages(client, root_url), (
        "the reply is unreachable by following the next-links"
    )
    _, msg_url = seeded["m6"]
    canonical = _canonical(client.get(msg_url).get_data(as_text=True))
    assert canonical is not None and canonical.endswith(msg_url), (
        f"an unrankable thread's reply claimed page {canonical}"
    )
