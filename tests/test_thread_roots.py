"""The materialised-thread-root invariant (W8).

`article_lists.thread_root_id` must equal the id of whatever
`find_thread_root` returns for that `(article, inbox)` pair. That one
sentence is the whole contract; this file exists to prove it holds
across the shapes that actually break it.

Written BEFORE the maintenance code, and deliberately parametrised over
three independent axes, because every blocking bug in the sibling W1b
work lived in an interaction the then-current guard held fixed:

- **thread shape** (linear, branched, deep, singleton, off-list parent,
  diamond, and cycles),
- **arrival order** (in-order, reverse, parent-last), because a child
  ingested before its parent starts self-rooted and must be re-rooted
  when the parent lands, and
- **inbox shape** (single, fully cross-posted, partial membership),
  because the root is per-inbox and a cross-posted reply routinely
  hangs off a root present in only one of its inboxes.

The oracle is `_find_thread_root_cte`, never `find_thread_root`. Once
the latter reads the column, using it here reduces every assertion to
`root_id == root_id`: measured, that silently cost 14 of 17 detections
against a write-path mutation. The whole point of this file is to
recompute the answer independently.

Cycles carry an explicit carve-out, see `CYCLIC`.
"""

import pytest
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, Inbox
from mimir.thread_roots import backfill_inbox
from mimir.threading import _find_thread_root_cte
from tests.test_routes._helpers import seed_thread_shape

# Shape -> ordered (message_id, parent) edges. The list order IS the
# ingest order, so re-ordering a shape's edges exercises arrival order.
SHAPES = {
    "linear": [("a1@x", None), ("a2@x", "a1@x"), ("a3@x", "a2@x")],
    "branched": [
        ("b1@x", None),
        ("b2@x", "b1@x"),
        ("b3@x", "b1@x"),
        ("b4@x", "b2@x"),
    ],
    "deep": [(f"c{i}@x", None if i == 1 else f"c{i - 1}@x") for i in range(1, 7)],
    "singleton": [("d1@x", None)],
    "off-list-parent": [("e1@x", "gone@elsewhere"), ("e2@x", "e1@x")],
    "self-parent": [("g1@x", "g1@x")],
    "cycle-2": [("h1@x", "h2@x"), ("h2@x", "h1@x")],
}

# Shapes where the graph contains a cycle. `find_thread_root` walks to
# MAX_DEPTH rather than to a fixed point, so its answer depends on
# `1000 mod cycle_length` and is not a root in any meaningful sense. A
# materialised column cannot reproduce that and should not try, so the
# column and `find_thread_root` DISAGREE here by design and `doctor
# thread-roots` must not report it as corruption.
#
# What IS required is that the column stays coherent: every member of a
# cycle resolves to the SAME root, and that root is itself a member of
# the cycle. That is what "inherit the parent's root, else self"
# converges on, it is deterministic and stable under re-ingest, and it
# keeps the cycle a single thread rather than fragmenting it. Demanding
# self-rooting instead would need a cycle detector on the write path
# for no gain a reader could observe.
CYCLIC = {"self-parent", "cycle-2"}

ORDERS = {
    "in-order": lambda edges: edges,
    "reverse": lambda edges: list(reversed(edges)),
    # Parent last: the case CONTEXT.md cites as the reason materialised
    # roots were deferred. Every child is self-rooted on arrival and
    # must be re-rooted when the parent finally lands.
    "parent-last": lambda edges: edges[1:] + edges[:1],
}


def _roots_by_inbox(
    inbox_name: str, only: set[str] | None = None
) -> dict[str, tuple[int | None, int]]:
    """`message_id -> (thread_root_id, article_id)` for one inbox.

    Scoped to `only` so the shape under test is not drowned by the
    shared conftest corpus, which seeds its own rows (with roots, as
    production always has).
    """
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        rows = s.execute(
            select(Article.message_id, ArticleList.thread_root_id, Article.id)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(ArticleList.inbox_id == inbox.id)
        ).all()
        return {
            mid: (root_id, art_id)
            for mid, root_id, art_id in rows
            if only is None or mid in only
        }


def _assert_invariant(inbox_name: str, shape: str) -> None:
    """The contract, recomputed rather than assumed."""
    materialised = _roots_by_inbox(inbox_name, {mid for mid, _ in SHAPES[shape]})
    assert materialised, f"{shape}: nothing ingested into {inbox_name}"

    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        for mid, (root_id, art_id) in materialised.items():
            if shape in CYCLIC:
                # Carve-out: coherence, not agreement with the CTE.
                assert root_id is not None, f"{shape}/{inbox_name}: {mid} unrooted"
                member_ids = {aid for _r, aid in materialised.values()}
                assert root_id in member_ids, (
                    f"{shape}/{inbox_name}: {mid} roots at {root_id}, "
                    "which is outside the cycle"
                )
                shared = {r for r, _a in materialised.values()}
                assert len(shared) == 1, (
                    f"{shape}/{inbox_name}: cycle fragmented across roots {shared}"
                )
                continue
            expected_mid = _find_thread_root_cte(s, inbox, mid)
            expected_id = materialised[expected_mid][1] if expected_mid else art_id
            assert root_id == expected_id, (
                f"{shape}/{inbox_name}: {mid} has thread_root_id={root_id}, "
                f"but find_thread_root says {expected_mid} (id={expected_id})"
            )


@pytest.mark.parametrize("order", sorted(ORDERS))
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_thread_root_matches_find_thread_root(client, tmp_path, shape, order):
    """Single inbox, every shape, every arrival order."""
    edges = ORDERS[order](SHAPES[shape])
    seed_thread_shape(tmp_path, "alpha", edges)
    _assert_invariant("alpha", shape)


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_thread_root_is_per_inbox_on_full_cross_post(client, tmp_path, shape):
    """Fully cross-posted: both inboxes see the same conversation, so
    both must resolve the same roots independently."""
    seed_thread_shape(tmp_path, "alpha", SHAPES[shape])
    _cross_post_all("alpha", "beta", only=[mid for mid, _ in SHAPES[shape]])
    _assert_invariant("alpha", shape)
    _assert_invariant("beta", shape)


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_thread_root_is_per_inbox_on_partial_membership(client, tmp_path, shape):
    """The case a per-ARTICLE column gets wrong for every cross-post:
    only the non-root messages are cross-posted, so in `beta` they hang
    off a parent that isn't there and must root differently than they
    do in `alpha`."""
    edges = SHAPES[shape]
    seed_thread_shape(tmp_path, "alpha", edges)
    if len(edges) > 1:
        _cross_post_all("alpha", "beta", only=[mid for mid, _ in edges[1:]])
        _assert_invariant("beta", shape)
    _assert_invariant("alpha", shape)


def _cross_post_all(src: str, dst: str, only: list[str] | None = None) -> None:
    """Link `src`'s articles into `dst`, reusing the same mirror and
    blob pointers so both inboxes resolve the same messages."""
    with SessionLocal() as s:
        source = s.execute(select(Inbox).where(Inbox.name == src)).scalar_one()
        target = s.execute(select(Inbox).where(Inbox.name == dst)).scalar_one()
        target.mirror_path = source.mirror_path
        links = s.execute(
            select(ArticleList, Article.message_id)
            .join(Article, Article.id == ArticleList.article_id)
            .where(ArticleList.inbox_id == source.id)
        ).all()
        for link, mid in links:
            if only is not None and mid not in only:
                continue
            s.add(
                ArticleList(
                    article_id=link.article_id,
                    inbox_id=target.id,
                    epoch=link.epoch,
                    commit_sha=link.commit_sha,
                )
            )
        s.commit()
        # Links created outside ingest land with a NULL root ("not yet
        # computed"), which is precisely the state the backfill exists
        # for, so run it here rather than hand-computing: that way this
        # test exercises the backfill path as well as the ingest one.
        backfill_inbox(s, target.id)
        s.commit()


def test_cross_post_ingested_separately_roots_per_inbox(client, tmp_path):
    """The cross-post path as INGEST actually produces it.

    Every other test here creates the second inbox's rows by direct
    insert plus backfill, which leaves the ingest-side inbox scoping
    unexercised: dropping the `inbox_id` filter from the parent lookup
    passed the whole suite. mimir ingests each inbox's mirror
    separately, so a cross-posted message really arrives twice, and the
    second arrival must resolve its parent within ITS OWN inbox.

    Here `beta`'s mirror carries only the reply. Its parent exists in
    the database (alpha ingested it) but not in beta, so in beta the
    reply is its own root while in alpha it roots at the real root.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    seed_thread_shape(
        tmp_path / "a", "alpha", [("xp-root@x", None), ("xp-reply@x", "xp-root@x")]
    )
    # Same message-id, different mirror: this is a cross-post, not a
    # new article, so ingest links the existing row into beta.
    seed_thread_shape(tmp_path / "b", "beta", [("xp-reply@x", "xp-root@x")])

    alpha = _roots_by_inbox("alpha", {"xp-root@x", "xp-reply@x"})
    beta = _roots_by_inbox("beta", {"xp-reply@x"})

    root_id = alpha["xp-root@x"][1]
    reply_id = alpha["xp-reply@x"][1]

    assert alpha["xp-reply@x"][0] == root_id, "in alpha the reply hangs off the root"
    assert beta["xp-reply@x"][0] == reply_id, (
        "in beta the parent is absent, so the reply must be its own root; "
        "inheriting alpha's root here would make the column inbox-blind"
    )


def test_verify_detects_a_corrupted_root(client, tmp_path):
    """The verifier is the only thing that can see this failure.

    A wrong `thread_root_id` splits a conversation while both halves
    keep rendering and nothing errors, so there is no symptom to notice
    and no exception to catch. Corrupt one row by hand and confirm the
    recompute catches it, and that a clean corpus reports nothing.
    """
    from sqlalchemy import select, update

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from mimir.thread_roots import verify_thread_roots

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("vr1@x", None), ("vr2@x", "vr1@x"), ("vr3@x", "vr2@x")]
    )

    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        assert verify_thread_roots(s, inbox) == [], "clean corpus reported mismatches"

        victim = s.execute(
            select(Article.id).where(Article.message_id == "vr3@x")
        ).scalar_one()
        # Point it at itself: the shape a lost re-rooting produces, and
        # the one that silently splits the thread in two.
        s.execute(
            update(ArticleList)
            .where(
                ArticleList.article_id == victim,
                ArticleList.inbox_id == inbox.id,
            )
            .values(thread_root_id=victim)
        )
        s.commit()

        found = verify_thread_roots(s, inbox)

    assert len(found) == 1, f"corruption not detected: {found}"
    assert found[0]["message_id"] == "vr3@x"
    assert found[0]["stored_root"] == victim
    assert found[0]["expected_root"] == seeded["vr1@x"][0]


def test_ingest_into_an_unbackfilled_corpus_never_writes_a_wrong_root(
    client,
    tmp_path,
):
    """The deploy sequence, which is the reachable path to corruption.

    The broker runs `alembic upgrade head` at startup and `mimir-tasks`
    starts firing `update` on its cadence, while `backfill-thread-roots`
    is a manual operator action that may be hours later. So for a while
    EVERY existing row is NULL, and replies keep arriving.

    Inheriting the parent's ARTICLE ID in that window looks harmless
    (it is non-NULL and plausible) but is wrong whenever the parent is
    not itself a root, and because it is non-NULL both `seed_roots` and
    `propagate` skip it forever: the backfill can never repair it. That
    is silent, permanent thread fragmentation.

    NULL is the correct answer while the parent is unresolved: readers
    fall back to the recursive CTE, and the backfill fixes it.
    """
    from sqlalchemy import select, update

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList, Inbox
    from mimir.thread_roots import backfill_inbox

    (tmp_path / "a").mkdir()
    seed_thread_shape(
        tmp_path / "a",
        "alpha",
        [("dep1@x", None), ("dep2@x", "dep1@x"), ("dep3@x", "dep2@x")],
    )
    # Simulate the post-migration state: every root unset.
    with SessionLocal() as s:
        s.execute(update(ArticleList).values(thread_root_id=None))
        s.commit()

    # An `update` tick lands a reply to a message whose root is unset.
    (tmp_path / "b").mkdir()
    seed_thread_shape(tmp_path / "b", "alpha", [("dep4@x", "dep3@x")])

    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        backfill_inbox(s, inbox.id)
        s.commit()

    _assert_invariant_for(
        "alpha", {"dep1@x", "dep2@x", "dep3@x", "dep4@x"}, "post-migration ingest"
    )


def _assert_invariant_for(inbox_name: str, mids: set[str], label: str) -> None:
    """The invariant for an explicit message-id set (shapes built
    across several `seed_thread_shape` calls aren't in `SHAPES`)."""
    materialised = _roots_by_inbox(inbox_name, mids)
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        for mid, (root_id, art_id) in materialised.items():
            expected_mid = _find_thread_root_cte(s, inbox, mid) or mid
            expected_id = materialised.get(expected_mid, (None, art_id))[1]
            assert root_id == expected_id, (
                f"{label}: {mid} has thread_root_id={root_id}, "
                f"but find_thread_root says {expected_mid} (id={expected_id})"
            )


def test_descendants_reroot_is_scoped_to_one_inbox(client, tmp_path):
    """The re-root must not reach into other inboxes.

    This is the same class as the parent-lookup scoping bug found by
    mutation testing, still open on the UPDATE side: dropping
    `inbox_id` from the descendants statement re-roots a subtree in
    EVERY inbox that carries it, which is wrong wherever the parent is
    absent from one of them.

    `beta` holds only the reply, so in beta it is its own root. `alpha`
    ingests the reply first and the root last, which fires the re-root
    in alpha; beta must be untouched.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    seed_thread_shape(tmp_path / "b", "beta", [("sc-reply@x", "sc-root@x")])
    seed_thread_shape(
        tmp_path / "a", "alpha", [("sc-reply@x", "sc-root@x"), ("sc-root@x", None)]
    )

    alpha = _roots_by_inbox("alpha", {"sc-root@x", "sc-reply@x"})
    beta = _roots_by_inbox("beta", {"sc-reply@x"})

    assert alpha["sc-reply@x"][0] == alpha["sc-root@x"][1], (
        "in alpha the late root must claim its descendant"
    )
    assert beta["sc-reply@x"][0] == beta["sc-reply@x"][1], (
        "in beta the root is absent, so the reply stays its own root; "
        "re-rooting it here would make the UPDATE inbox-blind"
    )


def test_reroot_does_not_walk_through_a_message_absent_from_this_inbox(
    client,
    tmp_path,
):
    """The descendants CTE's base case is inbox-scoped too.

    Chain root -> mid -> tail, with `mid` absent from alpha. In alpha
    the tail's parent is missing, so `find_thread_root` calls the tail
    its own root. If the walk can pass through `mid` anyway, the late
    root claims a grandchild it does not actually own here.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    # `mid` exists only in beta.
    seed_thread_shape(tmp_path / "b", "beta", [("wt-mid@x", "wt-root@x")])
    seed_thread_shape(
        tmp_path / "a",
        "alpha",
        [("wt-tail@x", "wt-mid@x"), ("wt-root@x", None)],
    )

    alpha = _roots_by_inbox("alpha", {"wt-root@x", "wt-tail@x"})
    assert alpha["wt-tail@x"][0] == alpha["wt-tail@x"][1], (
        "the tail's parent is absent from alpha, so it is its own root; "
        "walking through an absent message would hand it to the root"
    )


def test_seed_roots_is_inbox_scoped(client, tmp_path):
    """Direct unit assertion on `seed_roots`.

    An end-to-end test cannot pin this: when `seed_roots` wrongly skips
    a row, `break_cycle` used to self-root the stall and accidentally
    produce the right answer, so the bug hid behind another one. Assert
    the pass itself.
    """
    from sqlalchemy import select, update

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from mimir.thread_roots import seed_roots

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    seed_thread_shape(tmp_path / "b", "beta", [("ss-parent@x", None)])
    seed_thread_shape(tmp_path / "a", "alpha", [("ss-child@x", "ss-parent@x")])

    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        child = s.execute(
            select(Article.id).where(Article.message_id == "ss-child@x")
        ).scalar_one()
        s.execute(
            update(ArticleList)
            .where(ArticleList.inbox_id == alpha.id)
            .values(thread_root_id=None)
        )
        s.commit()

        seed_roots(s, alpha.id)
        s.commit()

        got = s.execute(
            select(ArticleList.thread_root_id).where(
                ArticleList.article_id == child,
                ArticleList.inbox_id == alpha.id,
            )
        ).scalar_one()

    assert got == child, (
        "the parent lives in another inbox, so this row is a root here "
        "and seed_roots must claim it"
    )


def test_child_first_batch_into_an_unbackfilled_corpus_leaves_nothing_stale(
    client,
    tmp_path,
):
    """The blocker's second form: a stale SIBLING rather than the
    arriving row.

    Post-migration everything is NULL. A batch arrives child-first (the
    `parent-last` axis, routine across epoch boundaries): the child
    self-roots correctly, then its parent arrives, finds ITS parent
    unrooted, and returns early. If that early return also skips the
    subtree, the child keeps a root that is now stale and non-NULL, so
    both backfill passes skip it forever.

    NULL has to propagate down the subtree, not just stop at the row.
    """
    from sqlalchemy import select, update

    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList, Inbox
    from mimir.thread_roots import backfill_inbox

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    seed_thread_shape(tmp_path / "a", "alpha", [("st-r@x", None), ("st-m@x", "st-r@x")])
    with SessionLocal() as s:
        s.execute(update(ArticleList).values(thread_root_id=None))
        s.commit()

    # One batch, child before parent.
    seed_thread_shape(
        tmp_path / "b", "alpha", [("st-b@x", "st-a@x"), ("st-a@x", "st-m@x")]
    )

    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        backfill_inbox(s, inbox.id)
        s.commit()

    _assert_invariant_for(
        "alpha",
        {"st-r@x", "st-m@x", "st-a@x", "st-b@x"},
        "child-first batch, unbackfilled corpus",
    )


def test_break_cycle_only_fires_on_a_real_cycle(client, tmp_path):
    """Pins HIGH-3's fix directly.

    Blind self-rooting of the lowest unrooted article writes a WRONG
    root on a plain chain, because the child routinely holds the lower
    id. Every other NULL row is rooted first here so `MIN(article_id)`
    cannot land on an unrelated one, which is what made a looser
    version of this test pass under the mutant.
    """
    from sqlalchemy import select, update

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from mimir.thread_roots import break_cycle

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    # Child ingested first, so it holds the lower article id.
    seed_thread_shape(tmp_path / "a", "alpha", [("bc-child@x", "bc-parent@x")])
    seed_thread_shape(tmp_path / "b", "alpha", [("bc-parent@x", "bc-root@x")])

    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        targets = {
            mid: aid
            for mid, aid in s.execute(select(Article.message_id, Article.id)).all()
        }
        # Root everything except the two-message chain, so the stall is
        # unambiguous and MIN() must pick one of them.
        s.execute(
            update(ArticleList)
            .where(ArticleList.inbox_id == inbox.id)
            .values(thread_root_id=ArticleList.article_id)
        )
        s.execute(
            update(ArticleList)
            .where(
                ArticleList.inbox_id == inbox.id,
                ArticleList.article_id.in_(
                    [targets["bc-child@x"], targets["bc-parent@x"]]
                ),
            )
            .values(thread_root_id=None)
        )
        s.commit()

        fired = break_cycle(s, inbox.id)
        s.commit()

    assert fired == 0, (
        "a non-cycle stall must be left alone; self-rooting the lowest "
        "article id writes a wrong root whenever the child holds it"
    )


def test_replay_resolves_roots_on_the_session_path(client, tmp_path):
    """Pins MEDIUM-4, on the branch that was silently a no-op.

    `SessionLocal` is autoflush=False and the passes are raw text()
    UPDATEs, so without an explicit flush they ran against a database
    that had not seen replay's ORM inserts and did nothing at all.

    Note this drives the helper DIRECTLY and simulates replay's inserts
    by hand, so it holds the mechanism axis fixed and cannot see
    anything about how `replay_failures` actually calls it. The
    subtree-adoption case is guarded through the real entry point in
    `tests/test_ingest/test_replay.py` instead.
    """
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox

    seed_thread_shape(
        tmp_path, "alpha", [("rp-root@x", None), ("rp-kid@x", "rp-root@x")]
    )
    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        beta.mirror_path = alpha.mirror_path
        # Link them into beta the way replay does: bare rows, no root.
        for mid in ("rp-root@x", "rp-kid@x"):
            link = s.execute(
                select(ArticleList)
                .join(Article, Article.id == ArticleList.article_id)
                .where(Article.message_id == mid, ArticleList.inbox_id == alpha.id)
            ).scalar_one()
            s.add(
                ArticleList(
                    article_id=link.article_id,
                    inbox_id=beta.id,
                    epoch=link.epoch,
                    commit_sha=link.commit_sha,
                )
            )
        from mimir.ingest.replay import _resolve_roots_after_replay

        s.flush()
        _resolve_roots_after_replay(s, beta.id, ["rp-root@x", "rp-kid@x"])
        s.commit()

    got = _roots_by_inbox("beta", {"rp-root@x", "rp-kid@x"})
    assert all(r is not None for r, _a in got.values()), (
        f"rows left unrooted on the session path: {got}"
    )


def test_verify_catches_corruption_downstream_of_a_cycle(client, tmp_path):
    """A clean tail hanging off a cycle is not itself cyclic.

    Skipping it outright let one crafted `In-Reply-To` loop exempt
    every message that ever replied into it, in the only detector this
    failure mode has. Full CTE agreement can't be demanded inside a
    cycle, but a row sharing its parent's root can be.
    """
    from sqlalchemy import select, update

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from mimir.thread_roots import verify_thread_roots

    seed_thread_shape(
        tmp_path,
        "alpha",
        [
            ("dc1@x", "dc2@x"),
            ("dc2@x", "dc1@x"),
            ("dc3@x", "dc1@x"),
            ("dc4@x", "dc3@x"),
        ],
    )
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        assert verify_thread_roots(s, inbox, limit=500) == []

        victim = s.execute(
            select(Article.id).where(Article.message_id == "dc4@x")
        ).scalar_one()
        s.execute(
            update(ArticleList)
            .where(
                ArticleList.article_id == victim,
                ArticleList.inbox_id == inbox.id,
            )
            .values(thread_root_id=victim)
        )
        s.commit()
        found = verify_thread_roots(s, inbox, limit=500)

    assert any(m["message_id"] == "dc4@x" for m in found), (
        f"corruption downstream of a cycle went unreported: {found}"
    )


def test_verify_samples_randomly_not_newest_first(client, tmp_path):
    """`ORDER BY id DESC` samples roughly the last hour of ingest, so a
    daily verify run structurally could not see corruption written
    during a deploy window, which is exactly the damage worth finding.
    Corrupt an OLD row and confirm a small sample reaches it."""
    from sqlalchemy import select, update

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from mimir.thread_roots import verify_thread_roots

    edges = [("rs1@x", None)] + [(f"rs{i}@x", f"rs{i - 1}@x") for i in range(2, 40)]
    seed_thread_shape(tmp_path, "alpha", edges)

    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        victim = s.execute(
            select(Article.id).where(Article.message_id == "rs2@x")
        ).scalar_one()
        s.execute(
            update(ArticleList)
            .where(
                ArticleList.article_id == victim,
                ArticleList.inbox_id == inbox.id,
            )
            .values(thread_root_id=victim)
        )
        s.commit()

        # Small samples, repeated: newest-first would never reach an
        # old row, random sampling reaches it with high probability.
        hits = sum(
            1
            for _ in range(60)
            if any(
                m["message_id"] == "rs2@x"
                for m in verify_thread_roots(s, inbox, limit=5)
            )
        )

    assert hits > 0, "a small sample never reached an old corrupted row"


def test_verify_does_a_full_recompute_not_just_coherence(client, tmp_path):
    """The verifier must not silently degrade to the weaker check.

    Adding the downstream-of-cycle coherence branch created a way for
    the strong check to disappear: if `_is_cyclic` starts returning
    True for ordinary rows (a shrunk `MAX_CYCLE_WALK` does exactly
    that), every row falls to coherence-only, which cannot see a
    subtree that is UNIFORMLY rooted at a wrong id. That shape is
    internally consistent, so each row agrees with its parent, and it
    is precisely what the original ingest bug produced.

    A mutant shrinking `MAX_CYCLE_WALK` to 1 was caught before the
    coherence branch existed and stopped being caught after, so this
    pins the recompute directly.
    """
    from sqlalchemy import select, update

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from mimir.thread_roots import verify_thread_roots

    seed_thread_shape(
        tmp_path,
        "alpha",
        [("fr1@x", None), ("fr2@x", "fr1@x"), ("fr3@x", "fr2@x")],
    )
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        wrong = s.execute(
            select(Article.id).where(Article.message_id == "fr3@x")
        ).scalar_one()
        ids = [
            aid
            for (aid,) in s.execute(
                select(Article.id).where(
                    Article.message_id.in_(["fr1@x", "fr2@x", "fr3@x"])
                )
            ).all()
        ]
        # Uniformly wrong: every row points at fr3, so parent/child
        # agree everywhere and coherence alone sees nothing.
        s.execute(
            update(ArticleList)
            .where(
                ArticleList.inbox_id == inbox.id,
                ArticleList.article_id.in_(ids),
            )
            .values(thread_root_id=wrong)
        )
        s.commit()
        found = verify_thread_roots(s, inbox, limit=500)

    assert len(found) >= 2, (
        f"a uniformly mis-rooted subtree needs the full recompute to see; "
        f"coherence alone reports {found}"
    )


def test_backfill_reports_exhaustion_when_the_pass_budget_runs_out(
    client,
    tmp_path,
    monkeypatch,
):
    """`exhausted` is the only thing distinguishing a truncated
    backfill from a complete one; both otherwise return the same
    counts."""
    from sqlalchemy import select

    from mimir import thread_roots
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox

    monkeypatch.setattr(thread_roots, "MAX_PASSES", 2)
    edges = [("ex1@x", None)] + [(f"ex{i}@x", f"ex{i - 1}@x") for i in range(2, 8)]
    seed_thread_shape(tmp_path, "alpha", edges)

    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.execute(
            __import__("sqlalchemy")
            .update(__import__("mimir.models", fromlist=["ArticleList"]).ArticleList)
            .values(thread_root_id=None)
        )
        s.commit()
        counts = thread_roots.backfill_inbox(s, inbox.id)
        s.commit()

    assert counts["exhausted"] == 1, counts


def test_backfill_handler_submits_one_writeop_per_pass(client, tmp_path):
    """c33765b's headline property: the writer is released between
    passes so web-tier cache writes drain instead of queueing behind a
    full-corpus run. Collapsing the passes back into one WriteOp is
    invisible to every other assertion."""
    from mimir.broker import _context
    from mimir.broker.handlers.maintenance import handle_backfill_thread_roots
    from mimir.broker.protocol import BackfillThreadRootsRequest

    seed_thread_shape(
        tmp_path, "alpha", [("wo1@x", None), ("wo2@x", "wo1@x"), ("wo3@x", "wo2@x")]
    )

    writer = _context.get_active_writer()
    labels: list[str] = []
    real_submit = writer.submit

    def _spy(op):
        labels.append(op.label)
        return real_submit(op)

    writer.submit = _spy  # type: ignore[method-assign]
    try:
        handle_backfill_thread_roots(
            BackfillThreadRootsRequest(rpc_id=1, inbox="alpha")
        )
    finally:
        writer.submit = real_submit  # type: ignore[method-assign]

    thread_ops = [x for x in labels if x.startswith("thread_roots:")]
    assert len(thread_ops) >= 2, f"passes were collapsed into one WriteOp: {labels}"
    assert thread_ops[0].startswith("thread_roots:seed_roots"), thread_ops


def test_cli_fails_when_the_backfill_is_truncated(client, tmp_path, monkeypatch):
    """A truncated backfill must not exit 0.

    The exhaustion warning only reaches the broker's own log, so
    without a non-zero exit the operator sees the same summary line as
    a complete run and has no reason to re-run.
    """
    from click.testing import CliRunner

    from mimir.cli.backfill import backfill_thread_roots_command

    monkeypatch.setattr(
        "mimir.broker.client.get_broker_client",
        lambda: type(
            "_C",
            (),
            {
                "backfill_thread_roots": staticmethod(
                    lambda **_kw: {
                        "inboxes": 1,
                        "seeded": 1,
                        "propagated": 0,
                        "cycles_broken": 0,
                        "exhausted": 1,
                    }
                )
            },
        )(),
    )

    result = CliRunner().invoke(backfill_thread_roots_command, [])
    assert result.exit_code != 0, result.output
    assert "pass budget" in result.output


def test_verifier_does_not_read_the_column_it_is_checking(client, tmp_path):
    """The verifier must recompute, not consult.

    `find_thread_root` now reads `thread_root_id` when populated, which
    is the whole point of the column. If the verifier used that entry
    point it would compare the column against itself and agree with any
    corruption by construction, silently turning the one detector for
    an invisible failure into a no-op.

    Corrupt a row and confirm both that the verifier still catches it
    AND that the fast path has genuinely been poisoned (so the test
    would fail if the verifier switched to `find_thread_root`).
    """
    from sqlalchemy import select, update

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from mimir.thread_roots import verify_thread_roots
    from mimir.threading import find_thread_root

    seed_thread_shape(
        tmp_path, "alpha", [("cv1@x", None), ("cv2@x", "cv1@x"), ("cv3@x", "cv2@x")]
    )
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        victim = s.execute(
            select(Article.id).where(Article.message_id == "cv3@x")
        ).scalar_one()
        s.execute(
            update(ArticleList)
            .where(
                ArticleList.article_id == victim,
                ArticleList.inbox_id == inbox.id,
            )
            .values(thread_root_id=victim)
        )
        s.commit()

        # The fast path now returns the corrupted answer...
        assert find_thread_root(s, inbox, "cv3@x") == "cv3@x"
        # ...and the verifier still reports the corruption, which it
        # could only do by recomputing independently.
        found = verify_thread_roots(s, inbox, limit=500)

    assert any(m["message_id"] == "cv3@x" for m in found), (
        f"verifier agreed with the corrupted column: {found}"
    )


@pytest.mark.parametrize("root_survives_elsewhere", [False, True])
def test_fast_path_ignores_a_root_no_longer_linked_to_this_inbox(
    client, tmp_path, root_survives_elsewhere
):
    """`find_thread_root` promises the topmost ancestor PRESENT IN THIS
    INBOX, and the fast path must keep that promise.

    A stored root can stop being a member without the FK's `SET NULL`
    firing: `reindex --from-scratch` drops an epoch's `article_lists`
    rows while the articles survive, so siblings keep pointing at a row
    that is no longer here. Answering with it would return a root the
    recursive walk would never give, and the caller would then render
    an empty thread.

    Parametrised over whether ANOTHER inbox still carries the root,
    because that is what makes the membership re-check's `inbox_id`
    scoping load-bearing. With the root gone from every inbox, an
    unscoped re-check misses it too and the bug is invisible; with a
    cross-post keeping it alive elsewhere (the common case on lkml,
    where nearly everything is cross-posted) an unscoped check happily
    answers with a root that belongs to a different inbox.
    """
    from sqlalchemy import delete, select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from mimir.threading import _find_thread_root_cte, find_thread_root

    seed_thread_shape(
        tmp_path, "alpha", [("fp1@x", None), ("fp2@x", "fp1@x"), ("fp3@x", "fp2@x")]
    )
    if root_survives_elsewhere:
        _cross_post_all("alpha", "beta", only=["fp1@x"])
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        root_id = s.execute(
            select(Article.id).where(Article.message_id == "fp1@x")
        ).scalar_one()
        # Unlink the root from this inbox; the article row survives, so
        # the children's thread_root_id still points at it.
        s.execute(
            delete(ArticleList).where(
                ArticleList.article_id == root_id,
                ArticleList.inbox_id == inbox.id,
            )
        )
        s.commit()

        fast = find_thread_root(s, inbox, "fp3@x")
        walk = _find_thread_root_cte(s, inbox, "fp3@x")

    assert fast == walk, (
        f"fast path answered {fast!r} with a root that is no longer in "
        f"this inbox; the walk says {walk!r}"
    )


@pytest.mark.parametrize("position", ["root", "middle", "leaf"])
def test_reindex_from_scratch_never_leaves_a_root_the_walk_cannot_reach(
    client, tmp_path, position
):
    """Drive the REAL `reindex --from-scratch`, across separate epochs.

    Two axes, both of which had been held fixed. The MECHANISM: every
    other test here simulates the destructive path with a direct DELETE,
    so nothing exercised the command that actually produces this state.
    The POSITION: the pre-existing fast-path guard only ever unlinked the
    ROOT, which is the one case the fast path's own re-check already
    covers. Unlinking a middle message leaves the root a member, so the
    re-check passes while the walk stops at the orphan, and the column
    and the walk disagree.

    The re-walk normally restores the removed message and the disagreement
    heals. This seeds the case where it does not, because the epoch's blob
    is gone, which is exactly when the damage is permanent.
    """
    from click.testing import CliRunner

    from mimir.cli.ingest import reindex_command

    # One message per epoch, so reindexing an epoch removes exactly one
    # link row and leaves the rest of the chain in place.
    chain = [("rx1@x", None), ("rx2@x", "rx1@x"), ("rx3@x", "rx2@x")]
    for i, edge in enumerate(chain):
        seed_thread_shape(tmp_path, "alpha", [edge], epoch=f"{i}.git")
    # A second thread in a second inbox, so the reset's `inbox_id`
    # scope has something to be wrong about.
    beta_mirror = tmp_path / "beta-mirror"
    beta_mirror.mkdir()
    other = seed_thread_shape(
        beta_mirror, "beta", [("rxb1@x", None), ("rxb2@x", "rxb1@x")]
    )
    other_before = _roots_by_inbox("beta", set(other))
    assert all(r is not None for r, _a in other_before.values())
    victim = {"root": 0, "middle": 1, "leaf": 2}[position]

    # Re-point the victim epoch at a repo that no longer carries its
    # message, so the re-walk cannot restore it.
    import shutil

    shutil.rmtree(tmp_path / f"{victim}.git")
    seed_thread_shape(tmp_path, "alpha", [("rx9@x", None)], epoch=f"{victim}.git")

    result = CliRunner().invoke(
        reindex_command, ["alpha", f"{victim}.git", "--from-scratch"]
    )
    assert result.exit_code == 0, result.output

    survivors = {mid for i, (mid, _p) in enumerate(chain) if i != victim}

    # Assert the STORED value against the independent oracle, not
    # `find_thread_root` against it. `find_thread_root` falls back to
    # the CTE whenever the column is NULL, so comparing the two is
    # satisfied by construction on exactly the state the reset
    # produces: an earlier version of this assertion stayed green with
    # the entire rebuild step deleted.
    _assert_invariant_for("alpha", survivors, f"reindex/{position}")
    assert _nulls_remaining("alpha") == 0, (
        f"reindexing the {position} epoch left rows unrooted; the rebuild "
        "did not run or did not finish"
    )
    # The other inbox must be untouched. The reset is whole-inbox, and
    # nothing here would notice it going whole-DATABASE: production is
    # ~200 inboxes and every test in this file drives one.
    assert _roots_by_inbox("beta", other_before.keys()) == other_before, (
        "reindexing alpha changed beta's roots"
    )


def test_broker_startup_backfills_thread_roots(client, tmp_path, monkeypatch):
    """The backfill runs itself, rather than relying on an operator.

    W8's column is read by `find_thread_root` and the sitemap. Readers
    fall back while a row is NULL, so an unfilled corpus is correct but
    under-reported: deploy without filling and the sitemap silently
    omits threads until somebody remembers the command. "Somebody
    remembers" is not a mechanism.

    It is sentinel-gated inside `build_server`, so it completes before
    `serve()` opens the socket and therefore before the web tier's
    healthcheck dependency is satisfied. This exercises the real
    startup function against a corpus rewound to the post-migration
    state.
    """
    from sqlalchemy import update

    from mimir.broker.server import _backfill_thread_roots_if_needed
    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList

    seeded = seed_thread_shape(
        tmp_path,
        "alpha",
        [("bs1@x", None), ("bs2@x", "bs1@x"), ("bs3@x", "bs2@x")],
    )
    mids = set(seeded)

    with SessionLocal() as s:
        s.execute(update(ArticleList).values(thread_root_id=None))
        s.commit()

    assert all(root is None for root, _a in _roots_by_inbox("alpha", mids).values())

    sentinel_dir = tmp_path / "sock"
    sentinel_dir.mkdir()
    _backfill_thread_roots_if_needed(sentinel_dir / "broker.sock")

    roots = _roots_by_inbox("alpha", mids)
    assert all(root is not None for root, _a in roots.values()), roots
    _assert_invariant_for("alpha", mids, "broker startup backfill")
    assert (sentinel_dir / ".thread_roots_backfilled").exists()

    # Second call with nothing to do is a no-op: the sentinel keeps
    # restarts cheap. Probe it by making the backfill explode, so a
    # short-circuit is the only way through.
    import mimir.thread_roots

    def _explode(*_a, **_kw):
        raise AssertionError("backfill ran despite the sentinel and a full column")

    monkeypatch.setattr(mimir.thread_roots, "drive_passes", _explode)
    _backfill_thread_roots_if_needed(sentinel_dir / "broker.sock")
    monkeypatch.undo()


def test_startup_backfill_reruns_when_the_sentinel_lies(client, tmp_path):
    """Sentinel present but rows NULL again: re-run, do not short-circuit.

    Reachable by rolling back to a pre-column image, whose ORM omits
    the column on INSERT, and then rolling forward. The sentinel would
    otherwise short-circuit forever and nothing else would notice:
    `verify_thread_roots` samples only non-NULL rows, so it is
    structurally blind to this, and the sitemap's root test IS the
    column, so those threads would silently vanish from it.
    """
    from sqlalchemy import update

    from mimir.broker.server import _backfill_thread_roots_if_needed
    from mimir.models import ArticleList

    seeded = seed_thread_shape(tmp_path, "alpha", [("sl1@x", None), ("sl2@x", "sl1@x")])
    mids = set(seeded)

    sock = tmp_path / "sock"
    sock.mkdir()
    sentinel = sock / ".thread_roots_backfilled"
    sentinel.touch()

    with SessionLocal() as s:
        s.execute(update(ArticleList).values(thread_root_id=None))
        s.commit()

    _backfill_thread_roots_if_needed(sock / "broker.sock")

    assert _nulls_remaining("alpha") == 0, (
        "a stale sentinel short-circuited a corpus that had gone unrooted"
    )
    _assert_invariant_for("alpha", mids, "stale-sentinel rerun")


def _null_all_roots():
    from sqlalchemy import update

    from mimir.models import ArticleList

    with SessionLocal() as s:
        s.execute(update(ArticleList).values(thread_root_id=None))
        s.commit()


def _nulls_remaining(inbox_name: str) -> int:
    from sqlalchemy import func

    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
        return s.execute(
            select(func.count())
            .select_from(ArticleList)
            .where(
                ArticleList.inbox_id == inbox.id,
                ArticleList.thread_root_id.is_(None),
            )
        ).scalar_one()


def test_startup_backfill_covers_every_inbox(client, tmp_path):
    """Production is ~200 inboxes; the original guard seeded one.

    With a single inbox, a startup pass that covers only the first and
    skips the rest is indistinguishable from a correct one, so
    `for inbox in inboxes[:1]` passed the entire suite.
    """
    from mimir.broker.server import _backfill_thread_roots_if_needed

    seeded = seed_thread_shape(tmp_path, "alpha", [("mi1@x", None), ("mi2@x", "mi1@x")])
    _cross_post_all("alpha", "beta", only=list(seeded))
    _null_all_roots()

    sock = tmp_path / "sock"
    sock.mkdir()
    _backfill_thread_roots_if_needed(sock / "broker.sock")

    for name in ("alpha", "beta"):
        assert _nulls_remaining(name) == 0, f"{name} was left unfilled"
        _assert_invariant_for(name, set(seeded), f"startup backfill/{name}")


def test_startup_backfill_withholds_the_sentinel_when_the_pass_budget_blows(
    client, tmp_path, monkeypatch
):
    """The sentinel is the ONLY thing that makes this retry, and the
    re-sample must not sit behind the success branch either.

    Touching the sentinel over a partial fill is permanent: no later
    restart re-runs, and every unrooted thread stays missing from the
    sitemap forever. Deleting the `else:` guarding `sentinel.touch()`
    passed the entire suite, which is exactly the silent-under-reporting
    this function exists to prevent.

    The ANALYZE assertion rides along on the same fixture because it is
    the same scenario, and a partial fill is the state MOST in need of
    fresh stats, not least: the column's distribution has just moved and
    ANALYZE may have sampled it while it was still entirely NULL, so the
    planner goes on treating it as single-valued. Priority inverted, the
    more incomplete the fill, the less likely the stats were refreshed.
    """
    import mimir.maintenance
    import mimir.thread_roots
    from mimir.broker.server import _backfill_thread_roots_if_needed

    calls = []
    monkeypatch.setattr(mimir.maintenance, "run_analyze", lambda **kw: calls.append(kw))

    seed_thread_shape(
        tmp_path, "alpha", [("pb1@x", None), ("pb2@x", "pb1@x"), ("pb3@x", "pb2@x")]
    )
    _null_all_roots()
    # One pass cannot propagate a three-deep chain, so the run exhausts.
    monkeypatch.setattr(mimir.thread_roots, "MAX_PASSES", 1)

    sock = tmp_path / "sock"
    sock.mkdir()
    _backfill_thread_roots_if_needed(sock / "broker.sock")

    assert _nulls_remaining("alpha") > 0, "test did not actually exhaust the budget"
    assert not (sock / ".thread_roots_backfilled").exists(), (
        "sentinel was written over a partial fill; no restart will ever retry"
    )
    assert calls, (
        "no ANALYZE after a partial fill; the planner keeps stats sampled "
        "while thread_root_id was entirely NULL and under-values the index"
    )


def test_startup_backfill_commits_per_inbox_and_withholds_on_failure(
    client, tmp_path, monkeypatch
):
    """A failure on the last inbox must not discard the first 199.

    The run committed once after the loop, so any error, SIGKILL, or
    OOM threw away every completed inbox: the opposite of the
    "interrupted runs resume" property `backfill_inbox` documents.
    """
    import mimir.thread_roots
    from mimir.broker.server import _backfill_thread_roots_if_needed

    seeded = seed_thread_shape(tmp_path, "alpha", [("pc1@x", None), ("pc2@x", "pc1@x")])
    _cross_post_all("alpha", "beta", only=list(seeded))
    _null_all_roots()

    with SessionLocal() as s:
        beta_id = s.execute(select(Inbox.id).where(Inbox.name == "beta")).scalar_one()

    real = mimir.thread_roots.seed_roots

    def _fail_on_beta(session, inbox_id):
        if inbox_id == beta_id:
            raise RuntimeError("injected failure on the second inbox")
        return real(session, inbox_id)

    # Patched at the pass level, because the startup path drives the
    # passes itself (to commit between them) rather than calling
    # `backfill_inbox`.
    monkeypatch.setattr(mimir.thread_roots, "seed_roots", _fail_on_beta)

    sock = tmp_path / "sock"
    sock.mkdir()
    _backfill_thread_roots_if_needed(sock / "broker.sock")

    assert _nulls_remaining("alpha") == 0, (
        "the failing inbox rolled back work that had already succeeded"
    )
    assert not (sock / ".thread_roots_backfilled").exists(), (
        "sentinel was written even though an inbox errored"
    )


def test_startup_verification_reports_a_mismatch(client, tmp_path, caplog):
    """The verification block is the only detector for a failure the
    docstring itself calls invisible, and deleting it wholesale passed
    the entire suite.

    Asserting on the log is the contract here: the block deliberately
    does not raise or gate startup, so the ERROR line IS its output.
    The broker CLI installs a root handler at INFO
    (`_configure_logging(max(verbose, 1))`), so unlike the web tier this
    line does reach the container log for the post-deploy smoke to grep.
    """
    import logging
    import threading

    from sqlalchemy import update

    from mimir.broker.server import _verify_thread_roots_async

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("vm1@x", None), ("vm2@x", "vm1@x"), ("vm3@x", "vm2@x")]
    )
    # Corrupt one row to a plausible-but-wrong root. The backfill only
    # touches NULLs, so it will leave this alone and verification is
    # what has to catch it.
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        wrong = seeded["vm3@x"][0]
        s.execute(
            update(ArticleList)
            .where(
                ArticleList.inbox_id == inbox.id,
                ArticleList.article_id == seeded["vm2@x"][0],
            )
            .values(thread_root_id=wrong)
        )
        s.commit()

    # Driven directly rather than through the backfill. Verification is
    # no longer the backfill's last statement, because being there meant
    # it only ever ran on the deploy that introduced the column: the
    # sentinel early-return skips the whole function on every later
    # restart. `build_server` calls it unconditionally instead, which
    # `test_startup_verification_runs_even_when_the_backfill_is_skipped`
    # pins.
    with caplog.at_level(logging.ERROR, logger="mimir.broker.server"):
        _verify_thread_roots_async().join(timeout=30)
        # Belt and braces if the thread was already replaced.
        for t in threading.enumerate():
            if t.name == "thread-roots-verify":
                t.join(timeout=30)

    assert any("verification found" in r.message for r in caplog.records), (
        f"verification did not report the corrupted root: {caplog.records}"
    )


def test_startup_verification_runs_even_when_the_backfill_is_skipped(
    client, tmp_path, monkeypatch
):
    """The detector must fire on EVERY start, not just the first one.

    Verification used to be the last statement of
    `_backfill_thread_roots_if_needed`, which meant it ran exactly once
    ever: that function returns early whenever its sentinel exists and no
    row is NULL, which is the state of every ordinary restart, and both
    of its failure paths `return` before reaching the end too. So the
    only detector for a failure the code itself calls invisible fired
    once, on the deploy that introduced the column, at the one moment the
    corpus was correct by construction. Every path that can corrupt the
    column fires strictly later.
    """
    from mimir.broker import server as broker_server
    from tests.test_broker.test_server import short_socket_path

    calls: list[bool] = []
    monkeypatch.setattr(
        broker_server, "_verify_thread_roots_async", lambda: calls.append(True)
    )

    sp = short_socket_path("verify-always")
    # Every sentinel present, so all four bootstrap steps short-circuit:
    # exactly the ordinary-restart state in which verification used to be
    # unreachable.
    for name in (
        ".migrated",
        ".bootstrapped",
        ".broker_initial_analyze",
        ".thread_roots_backfilled",
    ):
        (sp.parent / name).touch()

    server = broker_server.build_server(sp)
    try:
        assert calls, (
            "build_server skipped thread-root verification on a restart where "
            "every bootstrap sentinel was already present"
        )
    finally:
        server.server_close()
        if sp.exists():
            sp.unlink()


def test_find_incoherent_roots_catches_a_split_thread(client, tmp_path):
    """The complete structural check, which the sampling verifier cannot
    be relied on for.

    `verify_thread_roots` samples 200 rows per inbox, which on the
    production corpus is ~0.0007%, so a split confined to a few threads
    is overwhelmingly likely to go unsampled. Coherence is checked over
    every row instead: within one inbox a message and its parent belong
    to the same conversation, so they must name the same root.
    """
    from sqlalchemy import update

    from mimir.thread_roots import find_incoherent_roots

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("ic1@x", None), ("ic2@x", "ic1@x"), ("ic3@x", "ic2@x")]
    )

    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        assert find_incoherent_roots(s, inbox) == [], (
            "a healthy thread must be coherent; a false positive here would "
            "make the check useless noise"
        )

        # Split the thread: ic2 keeps its own id as root while its parent
        # ic1 still roots the conversation. This is exactly the shape a
        # repair pass skipping a non-NULL row leaves behind.
        s.execute(
            update(ArticleList)
            .where(
                ArticleList.inbox_id == inbox.id,
                ArticleList.article_id == seeded["ic2@x"][0],
            )
            .values(thread_root_id=seeded["ic2@x"][0])
        )
        s.commit()

        found = find_incoherent_roots(s, inbox)

    assert found, "a split thread went undetected by the coherence check"
    assert {f["article_id"] for f in found} == {
        seeded["ic2@x"][0],
        seeded["ic3@x"][0],
    }, (
        "expected both the split row and the child that now disagrees with "
        f"it, got {found}"
    )


def test_find_incoherent_roots_does_not_report_another_inbox(client, tmp_path):
    """Scoped to the inbox asked about.

    Threading is inbox-scoped, so the answer must be too. Losing the
    `al.inbox_id` filter still returns only within-inbox parent/child
    pairs (the join enforces that), which is why it is easy to miss:
    every row it reports is a genuine split, just not one belonging to
    the inbox being asked about. Two consequences, and the second is the
    expensive one. The log misattributes inbox B's damage to inbox A and
    repeats it once per inbox, and the query degrades from a scan of one
    inbox's rows to a full scan of `article_lists` performed once per
    inbox: ~200 passes over 28.8M rows on the production corpus instead
    of ~200 partial ones.
    """
    from sqlalchemy import update

    from mimir.thread_roots import find_incoherent_roots

    seeded = seed_thread_shape(tmp_path, "beta", [("xi1@x", None), ("xi2@x", "xi1@x")])

    with SessionLocal() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.execute(
            update(ArticleList)
            .where(
                ArticleList.inbox_id == beta.id,
                ArticleList.article_id == seeded["xi2@x"][0],
            )
            .values(thread_root_id=seeded["xi2@x"][0])
        )
        s.commit()

        assert find_incoherent_roots(s, beta), (
            "precondition: beta's thread really is split"
        )
        assert find_incoherent_roots(s, alpha) == [], (
            "beta's split was reported against alpha; the check is not inbox-scoped"
        )


def test_find_incoherent_roots_ignores_self_parents(client, tmp_path):
    """A self-referential In-Reply-To must not read as a split.

    Production had 1,360 of these when last measured, so a false positive
    on this shape would bury the real signal under noise on every start.

    Note this deliberately does NOT kill the `p.id != a.id` guard in the
    query: a self-parent resolves the parent row to the child row, so the
    root comparison is a row against itself and is false with or without
    the guard. The property is worth pinning even though the guard it
    looks like it protects is redundant here.
    """
    from mimir.thread_roots import find_incoherent_roots

    seed_thread_shape(tmp_path, "alpha", [("sp1@x", "sp1@x")])

    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        assert find_incoherent_roots(s, alpha) == [], (
            "a self-parent was reported as an incoherent root"
        )


def test_reindex_from_scratch_refuses_when_it_cannot_rebuild(client, tmp_path):
    """Never destroy in a context that cannot finish the job.

    `ingest_epoch` resolves its writer from the broker context, which
    only `serve()` sets, so a plain CLI process raises the moment the
    re-walk starts. The destructive half runs and COMMITS before that,
    so without a pre-check a `--from-scratch` in the wrong process
    deletes an epoch's links, blanks the inbox's roots, and dies. And
    nothing repairs it: the startup backfill is sentinel-gated, the
    scheduler has no thread-roots pass, and `verify_thread_roots` only
    samples non-NULL rows so it cannot see an all-NULL inbox.

    The suite hides this by default because conftest installs a broker
    context session-wide, so this clears it to get the production shape.
    """
    from click.testing import CliRunner

    from mimir.broker import _context
    from mimir.cli.ingest import reindex_command

    seed_thread_shape(tmp_path, "alpha", [("nb1@x", None)], epoch="0.git")
    seed_thread_shape(tmp_path, "alpha", [("nb2@x", "nb1@x")], epoch="1.git")
    mids = {"nb1@x", "nb2@x"}
    before = _roots_by_inbox("alpha", mids)
    assert all(r is not None for r, _a in before.values())

    pool, writer = _context.get_active_pool(), _context.get_active_writer()
    _context.clear_active()
    try:
        result = CliRunner().invoke(
            reindex_command, ["alpha", "1.git", "--from-scratch"]
        )
    finally:
        _context.set_active(pool, writer)

    assert result.exit_code != 0, result.output
    assert "active broker writer" in result.output, result.output
    assert _roots_by_inbox("alpha", mids) == before, (
        "refused the run but destroyed state on the way out"
    )
    assert _nulls_remaining("alpha") == 0


def test_reindex_from_scratch_rebuilds_even_when_the_rewalk_fails(
    client, tmp_path, monkeypatch
):
    """The failure path is the one that matters.

    A re-walk that dies partway (bad blob, OOM, Ctrl-C) would otherwise
    leave the whole inbox unrooted with nothing scheduled to repair it,
    which is strictly worse than the stale roots this command exists to
    clear.
    """
    from click.testing import CliRunner

    import mimir.cli.ingest as cli_ingest
    from mimir.cli.ingest import reindex_command

    seed_thread_shape(tmp_path, "alpha", [("rf1@x", None)], epoch="0.git")
    seed_thread_shape(tmp_path, "alpha", [("rf2@x", "rf1@x")], epoch="1.git")
    seed_thread_shape(tmp_path, "alpha", [("rf3@x", "rf2@x")], epoch="2.git")

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated mid-walk failure")

    monkeypatch.setattr(cli_ingest, "ingest_epoch", _boom)
    result = CliRunner().invoke(reindex_command, ["alpha", "1.git", "--from-scratch"])

    assert result.exit_code != 0
    assert _nulls_remaining("alpha") == 0, (
        "a failed re-walk left the inbox unrooted with nothing to repair it"
    )
    _assert_invariant_for("alpha", {"rf1@x", "rf3@x"}, "reindex/failed-rewalk")


def test_reindex_from_scratch_leaves_roots_alone_when_nothing_was_deleted(
    client, tmp_path
):
    """A mistyped epoch must not blank the inbox.

    `9.git` is a valid epoch name that this inbox never ingested, so
    the delete matches nothing. Resetting anyway would destroy every
    root in the inbox on a typo.
    """
    from click.testing import CliRunner
    from dulwich.repo import Repo

    from mimir.cli.ingest import reindex_command

    seed_thread_shape(tmp_path, "alpha", [("nd1@x", None)], epoch="0.git")
    seed_thread_shape(tmp_path, "alpha", [("nd2@x", "nd1@x")], epoch="1.git")
    mids = {"nd1@x", "nd2@x"}
    before = _roots_by_inbox("alpha", mids)

    Repo.init_bare(str(tmp_path / "9.git"), mkdir=True)
    result = CliRunner().invoke(reindex_command, ["alpha", "9.git", "--from-scratch"])

    assert "deleted 0 existing inbox-links" in result.output, result.output
    assert _roots_by_inbox("alpha", mids) == before, (
        "reset the inbox's roots even though nothing was deleted"
    )


def test_reindex_from_scratch_fails_loudly_on_a_truncated_rebuild(
    client, tmp_path, monkeypatch
):
    """Exit non-zero when the rebuild is truncated.

    Same reasoning as `mimir backfill-thread-roots`: without this the
    summary line is identical to a complete run and reads as done. It
    matters more here, because this command is what created the NULLs.
    """
    from click.testing import CliRunner

    import mimir.thread_roots
    from mimir.cli.ingest import reindex_command

    seed_thread_shape(tmp_path, "alpha", [("tr1@x", None)], epoch="0.git")
    seed_thread_shape(tmp_path, "alpha", [("tr2@x", "tr1@x")], epoch="1.git")
    seed_thread_shape(tmp_path, "alpha", [("tr3@x", "tr2@x")], epoch="2.git")
    monkeypatch.setattr(mimir.thread_roots, "MAX_PASSES", 1)

    result = CliRunner().invoke(reindex_command, ["alpha", "1.git", "--from-scratch"])

    assert result.exit_code != 0, result.output
    assert "pass budget" in result.output, result.output


def test_verification_completion_line_counts_match_the_errors_it_logged(
    client, tmp_path, caplog, monkeypatch
):
    """Pin each counter to the errors it claims to summarise.

    Nothing tied either label to its value: swapping the two arguments
    in the log call passed the entire suite. Both read 0 on a clean
    corpus, which is every other test's corpus, so the labels could be
    wrong in production and no test would care.

    Run ASYMMETRICALLY, in both directions, which is the whole point. A
    corrupted root trips BOTH detectors, giving split=1 mismatched=1,
    and against that a swap is undetectable: the first version of this
    test asserted the counts and still passed the swap mutant. Each
    direction here suppresses one detector so the two counts differ.
    """
    import logging
    import re
    import threading

    from sqlalchemy import update

    import mimir.thread_roots
    from mimir.broker.server import _verify_thread_roots_async

    seeded = seed_thread_shape(
        tmp_path, "alpha", [("wm1@x", None), ("wm2@x", "wm1@x"), ("wm3@x", "wm2@x")]
    )
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.execute(
            update(ArticleList)
            .where(
                ArticleList.inbox_id == inbox.id,
                ArticleList.article_id == seeded["wm2@x"][0],
            )
            .values(thread_root_id=seeded["wm3@x"][0])
        )
        s.commit()

    def _counts_after_run() -> tuple[int, int]:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="mimir.broker.server"):
            _verify_thread_roots_async().join(timeout=30)
            for t in threading.enumerate():
                if t.name == "thread-roots-verify":
                    t.join(timeout=30)
        messages = [r.getMessage() for r in caplog.records]
        done = [m for m in messages if "verification complete" in m]
        assert done, f"no completion line; got:\n{messages}"
        return (
            int(re.search(r"\bsplit_inboxes=(\d+)", done[0]).group(1)),
            int(re.search(r"\bmismatched_inboxes=(\d+)", done[0]).group(1)),
        )

    # Suppress the coherence check: only the sampled verifier may fire.
    monkeypatch.setattr(mimir.thread_roots, "find_incoherent_roots", lambda *a, **k: [])
    split, mismatched = _counts_after_run()
    assert (split, mismatched) == (0, 1), (
        "with splits suppressed the line must read split_inboxes=0 and "
        f"mismatched_inboxes=1; got split={split} mismatched={mismatched}"
    )

    # And the mirror image: only the coherence check may fire.
    monkeypatch.undo()
    monkeypatch.setattr(mimir.thread_roots, "verify_thread_roots", lambda *a, **k: [])
    split, mismatched = _counts_after_run()
    assert (split, mismatched) == (1, 0), (
        "with mismatches suppressed the line must read split_inboxes=1 "
        f"and mismatched_inboxes=0; got split={split} mismatched={mismatched}"
    )


def test_verification_failure_line_counts_only_inboxes_it_verified(
    client, tmp_path, caplog, monkeypatch
):
    """A first-inbox failure has verified nothing, and must say so.

    The counter was incremented at the top of the loop, so an inbox that
    raised was still counted as checked and the failure line read "after
    1 inbox(es)" having verified none. Moving it shipped unguarded, and
    a faithful revert passed the whole suite.
    """
    import logging
    import threading

    import mimir.thread_roots
    from mimir.broker.server import _verify_thread_roots_async

    seed_thread_shape(tmp_path, "alpha", [("xm1@x", None), ("xm2@x", "xm1@x")])

    def _explode(*_a, **_kw):
        raise RuntimeError("simulated failure on the very first inbox")

    monkeypatch.setattr(mimir.thread_roots, "find_incoherent_roots", _explode)

    with caplog.at_level(logging.INFO, logger="mimir.broker.server"):
        _verify_thread_roots_async().join(timeout=30)
        for t in threading.enumerate():
            if t.name == "thread-roots-verify":
                t.join(timeout=30)

    messages = [r.getMessage() for r in caplog.records]
    failed = [m for m in messages if "verification failed to run" in m]
    assert failed, f"no failure line; got:\n{messages}"
    assert "after 0 inbox(es)" in failed[0], (
        f"counted an inbox that raised as verified; got: {failed[0]}"
    )
