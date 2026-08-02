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

from tests.test_routes._helpers import build_thread


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
