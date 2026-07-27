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
        _resolve_roots_after_replay(s, beta.id)
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


def test_fast_path_ignores_a_root_no_longer_linked_to_this_inbox(client, tmp_path):
    """`find_thread_root` promises the topmost ancestor PRESENT IN THIS
    INBOX, and the fast path must keep that promise.

    A stored root can stop being a member without the FK's `SET NULL`
    firing: `reindex --from-scratch` drops an epoch's `article_lists`
    rows while the articles survive, so siblings keep pointing at a row
    that is no longer here. Answering with it would return a root the
    recursive walk would never give, and the caller would then render
    an empty thread.
    """
    from sqlalchemy import delete, select

    from mimir.extensions import SessionLocal
    from mimir.models import Article, ArticleList, Inbox
    from mimir.threading import _find_thread_root_cte, find_thread_root

    seed_thread_shape(
        tmp_path, "alpha", [("fp1@x", None), ("fp2@x", "fp1@x"), ("fp3@x", "fp2@x")]
    )
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


def test_broker_startup_backfills_thread_roots(client, tmp_path):
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
    from sqlalchemy import select, update

    from mimir.broker.server import _backfill_thread_roots_if_needed
    from mimir.extensions import SessionLocal
    from mimir.models import ArticleList, Inbox

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

    # Second call is a no-op: the sentinel keeps restarts cheap.
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.execute(
            update(ArticleList)
            .where(ArticleList.inbox_id == inbox.id)
            .values(thread_root_id=None)
        )
        s.commit()
    _backfill_thread_roots_if_needed(sentinel_dir / "broker.sock")
    assert all(root is None for root, _a in _roots_by_inbox("alpha", mids).values()), (
        "sentinel did not short-circuit the second run"
    )
