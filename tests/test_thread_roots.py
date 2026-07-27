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

Cycles carry an explicit carve-out, see `CYCLIC`.
"""

import pytest
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleList, Inbox
from mimir.thread_roots import backfill_inbox
from mimir.threading import find_thread_root
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
    # Two messages naming the same parent, then a third naming both
    # (References picks the last), so the graph is a DAG rather than a
    # tree from the ingest side.
    "diamond": [
        ("f1@x", None),
        ("f2@x", "f1@x"),
        ("f3@x", "f1@x"),
        ("f4@x", "f3@x"),
    ],
    "self-parent": [("g1@x", "g1@x")],
    "cycle-2": [("h1@x", "h2@x"), ("h2@x", "h1@x")],
    "cycle-3": [("i1@x", "i3@x"), ("i2@x", "i1@x"), ("i3@x", "i2@x")],
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
CYCLIC = {"self-parent", "cycle-2", "cycle-3"}

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

    Scoped to `only` because the shared conftest seeds articles
    directly rather than through ingest, so those rows legitimately
    carry NULL (the column means "not yet computed", which is what the
    backfill is for) and would otherwise drown the shape under test.
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
            expected_mid = find_thread_root(s, inbox, mid)
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


def test_verify_ignores_cycles(client, tmp_path):
    """Cycles are the one place the column and `find_thread_root`
    disagree on purpose (the CTE walks to MAX_DEPTH and lands wherever
    `1000 mod cycle_length` puts it). The verifier must not report that
    as corruption, or every run on a real corpus would cry wolf."""
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from mimir.thread_roots import verify_thread_roots

    seed_thread_shape(
        tmp_path, "alpha", [("cy1@x", "cy3@x"), ("cy2@x", "cy1@x"), ("cy3@x", "cy2@x")]
    )
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        assert verify_thread_roots(s, inbox) == []
