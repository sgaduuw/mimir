"""Guards for the invariants the thread-root machinery asserts in prose.

Every comment in this subsystem that says "therefore X holds" is a claim
nothing was checking. A wrong `thread_root_id` is invisible (pages
render, nothing errors, a conversation is quietly in two pieces), so a
prose invariant here decays in exactly the direction that costs most.

Three claims are pinned:

* `threading.unmaterialised_roots`'s depth-1 completeness argument, by
  ENUMERATION over generated states rather than by re-stating the
  argument. This is the load-bearing one: if depth-1 is incomplete the
  predicate reports a broken thread healthy, and the renderer, the
  canonical, the sitemap and IndexNow all then make page claims about a
  thread whose membership they cannot see.
* `_pending._resolve_thread_root`'s "the backfill can never repair a
  non-NULL row", which is the entire reason that branch writes NULL
  instead of the parent's id.
* `find_thread_root`'s "a verifier that reads the column it is checking
  would agree with any corruption by construction", which is why
  `verify_thread_roots` recomputes against `_find_thread_root_cte`.
"""

import itertools
import random

from sqlalchemy import select, text

from mimir.models import Article, ArticleList, Inbox
from mimir.threading import (
    _find_thread_root_cte,
    find_thread_root,
    unmaterialised_roots,
)

# Kept small on purpose. The enumeration is over (shape x membership x
# NULL pattern), so the state count is (n+1)^(n-1) * 2^n * 2^n and four
# nodes already covers every structural case the argument turns on: a
# root, a chain, a branch, a gap in the middle of a chain, a gap at the
# leaf, a wholly-unrooted thread, and a member whose parent is absent
# from this inbox.
_N = 4


def _seed_nodes(session, inbox_name, n):
    """One inbox and `n` parentless articles, all linked to it.

    Shape and NULL pattern are then imposed with UPDATEs, so the
    enumeration never pays an INSERT per configuration.
    """
    ix = Inbox(
        name=inbox_name,
        mirror_path=f"/nonexistent/{inbox_name}",
        upstream_url=f"https://example.invalid/{inbox_name}",
    )
    session.add(ix)
    session.flush()
    ids = []
    for i in range(n):
        art = Article(
            message_id=f"{inbox_name}-n{i}@x",
            subject="s",
            author="a@example.invalid",
            thread_parent=None,
            subject_normalized="s",
        )
        session.add(art)
        session.flush()
        ids.append(art.id)
        session.add(
            ArticleList(
                article_id=art.id,
                inbox_id=ix.id,
                epoch="0.git",
                commit_sha=f"{i:040d}",
            )
        )
    session.commit()
    return ix, ids


def _forests(n):
    """Every acyclic parent assignment over `n` labelled nodes.

    Yields `parent[i] -> j | None`. Cycles are excluded deliberately:
    `find_thread_root` walks to MAX_DEPTH rather than to a fixed point
    inside a loop, so "the thread rooted at R" is not defined there and
    the column is documented to disagree (see `thread_roots.break_cycle`
    and the CYCLIC carve-out in `tests/test_thread_roots.py`).
    """
    for combo in itertools.product(range(-1, n), repeat=n):
        parent = [None if p < 0 else p for p in combo]
        if any(parent[i] == i for i in range(n)):
            continue
        ok = True
        for i in range(n):
            seen = {i}
            cur = parent[i]
            while cur is not None:
                if cur in seen:
                    ok = False
                    break
                seen.add(cur)
                cur = parent[cur]
            if not ok:
                break
        if ok:
            yield parent


def _true_root(parent, members, i):
    """The topmost ancestor of `i` present in `members`.

    The Python model of `_find_thread_root_cte`: walk up
    `thread_parent`, stop at the last node this inbox actually carries.
    `test_python_truth_model_agrees_with_the_cte` holds the two
    together.
    """
    cur = i
    while True:
        p = parent[cur]
        if p is None or p not in members:
            return cur
        cur = p


def _apply(session, prefix, ix_id, ids, parent, members, nulls):
    """Impose one configuration: shape, membership, NULL pattern.

    `thread_parent` holds a message_id, not an id, so the numeric parent
    map is rewritten onto the generated message_ids here. Membership is
    imposed by deleting and re-inserting this inbox's `article_lists`
    rows, which is also what makes a non-member's absence real rather
    than simulated.
    """
    conn = session.connection()
    for i, art_id in enumerate(ids):
        p = parent[i]
        conn.execute(
            text("UPDATE articles SET thread_parent = :p WHERE id = :aid"),
            {"p": None if p is None else f"{prefix}-n{p}@x", "aid": art_id},
        )
    conn.execute(text("DELETE FROM article_lists WHERE inbox_id = :ix"), {"ix": ix_id})
    for i in sorted(members):
        root = None if i in nulls else ids[_true_root(parent, members, i)]
        conn.execute(
            text(
                "INSERT INTO article_lists "
                "(article_id, inbox_id, epoch, commit_sha, thread_root_id) "
                "VALUES (:aid, :ix, '0.git', :sha, :root)"
            ),
            {"aid": ids[i], "ix": ix_id, "sha": f"{i:040d}", "root": root},
        )


def _check_configuration(session, ix, ids, parent, members, nulls):
    """The property claim 6 is load-bearing for.

    For every root the predicate calls MATERIALISED, the set of rows the
    column attributes to it must be exactly the set of messages that
    genuinely belong to that thread in this inbox. A missing member is
    the failure that matters: the thread view renders only what the
    column attributes, while the message page still canonicalises the
    missing message onto one of that view's pages, so the canonical
    names a page that does not contain the content.

    Returns a description of the first violation, or None.
    """
    id_to_idx = {art_id: i for i, art_id in enumerate(ids)}
    true_members = {}
    for i in members:
        true_members.setdefault(_true_root(parent, members, i), set()).add(i)

    candidates = [i for i in sorted(members) if _true_root(parent, members, i) == i]
    if not candidates:
        return None

    batch = unmaterialised_roots(session, ix.id, [ids[i] for i in candidates])
    for i in candidates:
        single = unmaterialised_roots(session, ix.id, [ids[i]])
        if (ids[i] in batch) != (ids[i] in single):
            return (
                f"batched and single-root answers disagree for {i}: "
                f"parent={parent} members={sorted(members)} nulls={sorted(nulls)}"
            )
        if ids[i] in batch:
            continue  # correctly reported as unrankable
        column_members = {
            id_to_idx[row]
            for row in session.execute(
                select(ArticleList.article_id).where(
                    ArticleList.inbox_id == ix.id,
                    ArticleList.thread_root_id == ids[i],
                )
            ).scalars()
        }
        if column_members != true_members.get(i, set()):
            return (
                f"root {i} reported MATERIALISED but the column attributes "
                f"{sorted(column_members)} where the thread is "
                f"{sorted(true_members.get(i, set()))}; "
                f"parent={parent} members={sorted(members)} nulls={sorted(nulls)}"
            )
    return None


def test_python_truth_model_agrees_with_the_cte(session):
    """The enumeration's oracle is only as good as its model of
    `_find_thread_root_cte`. Hold the two together over every shape
    before trusting the model in the big enumeration."""
    ix, ids = _seed_nodes(session, "trmodel", _N)
    all_members = set(range(_N))
    checked = 0
    for parent in _forests(_N):
        _apply(session, "trmodel", ix.id, ids, parent, all_members, set())
        for i in range(_N):
            mid = f"trmodel-n{i}@x"
            expected = f"trmodel-n{_true_root(parent, all_members, i)}@x"
            assert _find_thread_root_cte(session, ix, mid) == expected, (
                f"model disagrees with the CTE for node {i}, parent={parent}"
            )
            checked += 1
    assert checked == 125 * _N, f"expected 500 comparisons, ran {checked}"


def test_unmaterialised_roots_depth1_is_complete_over_every_shape(session):
    """Claim: depth-1 is complete, so a thread the predicate calls
    materialised has no member the column cannot see.

    Exhaustive over every acyclic 4-node shape and every NULL pattern,
    with full membership. That space contains `reindex --from-scratch`
    (all rows NULL), a `drive_passes` budget exhaustion or a
    `break_cycle` stall (an arbitrary unrepaired subset), and the
    `_set_subtree_root(..., None)` shape (a contiguous descendant set),
    without having to trust that any of those produce only the patterns
    the argument reasons about.
    """
    ix, ids = _seed_nodes(session, "trdepth", _N)
    all_members = set(range(_N))
    configs = 0
    for parent in _forests(_N):
        for null_bits in range(1 << _N):
            nulls = {i for i in range(_N) if null_bits & (1 << i)}
            _apply(session, "trdepth", ix.id, ids, parent, all_members, nulls)
            problem = _check_configuration(session, ix, ids, parent, all_members, nulls)
            assert problem is None, problem
            configs += 1
    assert configs == 125 * 16, f"expected 2000 configurations, ran {configs}"


def test_unmaterialised_roots_holds_under_partial_inbox_membership(session):
    """Same claim, with the per-inbox axis varied.

    Threading is inbox-scoped, so a cross-posted reply routinely hangs
    off a parent this inbox does not carry, and the topmost node of an
    unrooted region can then have no in-inbox parent at all. Sampled
    rather than exhaustive because the space is shape x membership x
    NULL pattern; the seed is fixed so a failure reproduces.
    """
    ix, ids = _seed_nodes(session, "trmember", _N)
    forests = list(_forests(_N))
    rng = random.Random(20260803)
    for _ in range(1200):
        parent = rng.choice(forests)
        members = {i for i in range(_N) if rng.random() < 0.75}
        if not members:
            continue
        nulls = {i for i in members if rng.random() < 0.5}
        _apply(session, "trmember", ix.id, ids, parent, members, nulls)
        problem = _check_configuration(session, ix, ids, parent, members, nulls)
        assert problem is None, problem


def test_repair_passes_never_touch_a_non_null_row(session):
    """Claim: a stale non-NULL root is skipped forever and the backfill
    can never repair it.

    That claim is why `_resolve_thread_root` writes NULL rather than the
    parent's id when the parent is present but unrooted, so it is worth
    a guard: if a future pass started rewriting non-NULL rows, the
    branch's whole justification would be gone, and if one started
    quietly repairing them the comment would be a lie in the other
    direction.
    """
    from mimir.thread_roots import backfill_inbox, break_cycle, propagate, seed_roots

    ix, ids = _seed_nodes(session, "trstale", 3)
    # n0 <- n1 <- n2, with n1 holding a stale self-root: exactly the
    # shape `_resolve_thread_root` refuses to create.
    _apply(session, "trstale", ix.id, ids, [None, 0, 1], {0, 1, 2}, {0, 1, 2})
    conn = session.connection()
    for art_id, root in ((ids[0], ids[0]), (ids[1], ids[1]), (ids[2], None)):
        conn.execute(
            text(
                "UPDATE article_lists SET thread_root_id = :r "
                "WHERE inbox_id = :ix AND article_id = :aid"
            ),
            {"r": root, "ix": ix.id, "aid": art_id},
        )
    session.commit()

    assert seed_roots(session, ix.id) == 0
    # n2 does get a root, inherited from the stale parent, which is how
    # the corruption spreads rather than being repaired.
    propagate(session, ix.id)
    assert break_cycle(session, ix.id) == 0
    backfill_inbox(session, ix.id)
    session.commit()

    stored = dict(
        session.execute(
            select(ArticleList.article_id, ArticleList.thread_root_id).where(
                ArticleList.inbox_id == ix.id
            )
        ).all()
    )
    assert stored[ids[1]] == ids[1], (
        "a repair pass rewrote a non-NULL row; `_resolve_thread_root` "
        "writes NULL precisely because nothing does"
    )
    assert stored[ids[2]] == ids[1], (
        "the stale root propagated downward, which is the permanent "
        "corruption the NULL-instead-of-parent-id branch avoids"
    )


def test_verify_thread_roots_recomputes_rather_than_reading_the_column(session):
    """Claim: a verifier that reads the column it is checking would
    agree with any corruption by construction.

    Corrupt one row to a root that is still a live member of this inbox
    (so `find_thread_root`'s membership re-check passes and its fast
    path returns the corrupt value verbatim), then assert the two
    disagree: `find_thread_root` echoes the corruption, and
    `verify_thread_roots` reports it.
    """
    from mimir.thread_roots import verify_thread_roots

    ix, ids = _seed_nodes(session, "trverify", 3)
    # Two threads: n0 <- n1, and n2 alone. Point n1 at n2's thread.
    _apply(session, "trverify", ix.id, ids, [None, 0, None], {0, 1, 2}, {0, 1, 2})
    conn = session.connection()
    for art_id, root in ((ids[0], ids[0]), (ids[1], ids[2]), (ids[2], ids[2])):
        conn.execute(
            text(
                "UPDATE article_lists SET thread_root_id = :r "
                "WHERE inbox_id = :ix AND article_id = :aid"
            ),
            {"r": root, "ix": ix.id, "aid": art_id},
        )
    session.commit()

    assert find_thread_root(session, ix, "trverify-n1@x") == "trverify-n2@x", (
        "the fast path is expected to echo the stored value; if it stopped "
        "doing so this test no longer demonstrates anything"
    )
    assert _find_thread_root_cte(session, ix, "trverify-n1@x") == "trverify-n0@x"

    mismatches = verify_thread_roots(session, ix, limit=200)
    assert any(m["message_id"] == "trverify-n1@x" for m in mismatches), (
        "verify_thread_roots missed a corrupt root, which is what reading "
        "the column instead of recomputing would look like"
    )
