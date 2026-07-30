"""Backfill and verification for `article_lists.thread_root_id` (W8).

Ingest maintains the column incrementally (see
`mimir.ingest._pending._resolve_thread_root`); this module fills it in
for rows that predate the column, and proves it right afterwards.

The column is per-inbox because threading is: `find_thread_root` walks
within one inbox, so the same article can root differently in each
inbox it is linked to. Everything here is therefore scoped per inbox.
"""

import logging

from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# The passes take a Session OR a Core Connection: they only
# `execute(text(...))` and read `.rowcount`, which both support
# identically, and the broker handler runs them straight on the
# writer's Connection. Both are real callers, so the annotation says
# so rather than implying a wrapper is needed.

# Measured cost of a full run, the ONE canonical copy of this number.
# Three docstrings used to carry three different values for it (~2 min
# from an early guess, "~10 s per 500k", "2.3 s per 500k, linear"), and
# two of them were wrong; the drift is what this constant exists to
# stop. Benchmarked 2026-07-28 on a 1.2M-row / 200-inbox corpus with
# realistic thread depth: 11.4 s total.
FULL_RUN_SECONDS_AT_1M_ROWS = 11.4

# What the production corpus ACTUALLY held when last measured. Read
# straight off prod on 2026-07-28, not estimated:
#   203 inboxes, 17.3M articles, 28.8M article_lists rows.
#   Largest inbox lkml at 6.4M rows; 5 inboxes >= 1M, 55 in 100k-1M,
#   102 in 10k-100k, 41 below 10k.
#
# This constant exists because the previous figure was WRONG BY 5x and
# nobody noticed through a spec, several docstrings, two synthetic
# benchmarks and three independent reviews. Every one of them said
# "~6M rows", which is lkml ALONE mistaken for the whole corpus. The
# budget derived from it (a 180 s healthcheck `start_period`) would
# have aborted the deploy it was written for.
#
# Two rules follow, and they are why this is a named constant rather
# than prose:
#   1. It is a FLOOR with a date attached, never a fact. The corpus
#      only ever grows.
#   2. Anything that gates on it (the broker healthcheck window, a
#      migration's expected wall time, a sitemap urlset limit) must be
#      re-derived from a fresh measurement, not from this number's last
#      value. Re-measure with:
#        SELECT COUNT(*) FROM article_lists;
PROD_ARTICLE_LIST_ROWS_AT_2026_07_28 = 28_800_000

# Safety stop for the propagation loop. Real lkml threads rarely exceed
# ~50 deep and `threading.MAX_DEPTH` caps the read side at 1000, so a
# run that needs more passes than this has hit something pathological
# and should stop rather than spin.
MAX_PASSES = 1200

# Upper bound on a single verification walk up the parent chain.
# `threading.MAX_DEPTH` caps the read side at 1000, so anything
# deeper is pathological and treated as cyclic.
MAX_CYCLE_WALK = 1000


def seed_roots(session: Session | Connection, inbox_id: int) -> int:
    """Self-root every row whose parent is absent from this inbox.

    That is exactly the set `find_thread_root` calls a root: no
    `thread_parent` at all, or one naming a message this inbox does not
    carry (an off-list ancestor, or a cross-post whose root went to a
    different list).
    """
    return session.execute(
        text(
            """
            UPDATE article_lists AS al
               SET thread_root_id = al.article_id
             WHERE al.inbox_id = :ix
               AND al.thread_root_id IS NULL
               AND NOT EXISTS (
                   SELECT 1
                     FROM articles a
                     JOIN articles p ON p.message_id = a.thread_parent
                     JOIN article_lists pal ON pal.article_id = p.id
                    WHERE a.id = al.article_id
                      AND pal.inbox_id = al.inbox_id
                      AND p.id != a.id
               )
            """
        ),
        {"ix": inbox_id},
    ).rowcount


def propagate(session: Session | Connection, inbox_id: int) -> int:
    """Give every still-unrooted row its parent's root, one level."""
    return session.execute(
        text(
            """
            UPDATE article_lists AS al
               SET thread_root_id = (
                   SELECT pal.thread_root_id
                     FROM articles a
                     JOIN articles p ON p.message_id = a.thread_parent
                     JOIN article_lists pal ON pal.article_id = p.id
                    WHERE a.id = al.article_id
                      AND pal.inbox_id = al.inbox_id
                      AND pal.thread_root_id IS NOT NULL
                    LIMIT 1
               )
             WHERE al.inbox_id = :ix
               AND al.thread_root_id IS NULL
               AND EXISTS (
                   SELECT 1
                     FROM articles a
                     JOIN articles p ON p.message_id = a.thread_parent
                     JOIN article_lists pal ON pal.article_id = p.id
                    WHERE a.id = al.article_id
                      AND pal.inbox_id = al.inbox_id
                      AND pal.thread_root_id IS NOT NULL
               )
            """
        ),
        {"ix": inbox_id},
    ).rowcount


def break_cycle(session: Session | Connection, inbox_id: int) -> int:
    """Self-root one member of an actual cycle, if a stall is one.

    Propagation stalls when every remaining row is waiting on a parent
    that is itself waiting. That is USUALLY a cycle, but not always: a
    row can also be unrooted for an unrelated reason (a row inserted
    outside `seed_roots`'s view, or a concurrent ingest landing between
    passes). Self-rooting the lowest unrooted article blindly then
    produces a WRONG root rather than an arbitrary-but-coherent one,
    because on a plain chain the child routinely holds the lower
    article id (its parent arrived in a later epoch).

    So the stalled set is checked for membership in a cycle first, by
    walking `thread_parent` up from each candidate and seeing whether
    it returns to itself. Only a genuine cycle member is self-rooted,
    which lets the next propagation pass carry that root around the
    loop so the cycle converges on one member. A stall with no cycle in
    it is left alone: those rows stay NULL, which is a state readers
    handle (they fall back to the recursive CTE) and a later run can
    still repair.
    """
    rows = session.execute(
        text(
            """
            SELECT al.article_id, a.message_id, a.thread_parent
              FROM article_lists al
              JOIN articles a ON a.id = al.article_id
             WHERE al.inbox_id = :ix AND al.thread_root_id IS NULL
             ORDER BY al.article_id
            """
        ),
        {"ix": inbox_id},
    ).all()
    if not rows:
        return 0

    # Parent map restricted to this inbox: a chain leaving the inbox is
    # not a cycle here even if it loops elsewhere.
    parent_of = {mid: parent for _aid, mid, parent in rows}

    for article_id, mid, _parent in rows:
        seen = {mid}
        cur = parent_of.get(mid)
        while cur is not None:
            if cur == mid:
                # Walked back to where we started: a real cycle.
                return session.execute(
                    text(
                        "UPDATE article_lists SET thread_root_id = article_id "
                        "WHERE inbox_id = :ix AND article_id = :aid"
                    ),
                    {"ix": inbox_id, "aid": article_id},
                ).rowcount
            if cur in seen:
                break
            seen.add(cur)
            cur = parent_of.get(cur)
    return 0


def drive_passes(run) -> dict[str, int]:
    """Run the seed / propagate / break-cycle convergence to fixpoint.

    `run(pass_fn) -> rowcount` is the only thing the three callers
    disagree about: the backfill handler submits each pass as its own
    WriteOp so the single writer is released between them, while the
    in-session and replay callers just execute inline. Keeping one copy
    of the loop matters more than the six lines it saves, because the
    termination logic has to be right in all three and the copies had
    already drifted (the replay one had no exhaustion signal at all).
    """
    counts = {
        "seeded": run(seed_roots),
        "propagated": 0,
        "cycles_broken": 0,
        "exhausted": 0,
    }
    for _ in range(MAX_PASSES):
        moved = run(propagate)
        if moved:
            counts["propagated"] += moved
            continue
        if run(break_cycle):
            counts["cycles_broken"] += 1
            continue
        break
    else:
        counts["exhausted"] = 1
    return counts


def backfill_inbox(session: Session, inbox_id: int) -> dict[str, int]:
    """Populate `thread_root_id` for one inbox, all passes in ONE
    session. Idempotent: only touches NULL rows, so a re-run resumes a
    partial pass and never clobbers live ingest.

    For non-broker callers (tests, dev scripts) that want the whole run
    in one transaction. Both in-broker drivers instead commit one pass
    at a time (the RPC handler submits each as its own WriteOp; the
    startup path drives the same `drive_passes` seam and commits
    between passes), because holding the single writer for a full run
    would queue every web-tier cache write behind it. See
    `FULL_RUN_SECONDS_AT_1M_ROWS` for how long that is.
    """
    counts = drive_passes(lambda fn: fn(session, inbox_id))
    if counts["exhausted"]:
        logger.warning(
            "thread-roots: inbox %s hit MAX_PASSES (%s); rows remain unrooted",
            inbox_id,
            MAX_PASSES,
        )
    return counts


def find_incoherent_roots(session: Session, inbox, limit: int = 20) -> list[dict]:
    """Rows whose in-inbox parent carries a DIFFERENT root.

    A structural invariant, checked over EVERY row rather than a sample:
    within one inbox, a message and its parent belong to the same
    conversation, so they must name the same root. A violation means the
    thread is materialised in two pieces.

    This complements `verify_thread_roots` rather than replacing it, and
    the two catch disjoint failures:

      * Coherence (here) is COMPLETE but only sees a SPLIT. It cannot
        notice a thread that agrees internally and is rooted at the
        wrong article.
      * `verify_thread_roots` recomputes against an independent oracle,
        so it catches a coherent-but-wrong root, but it samples (200
        rows per inbox, which on this corpus is ~0.0007%), so it will
        usually miss a split confined to a handful of threads.

    Splits are the failure that maintenance bugs actually produce (a
    repair pass skipping a non-NULL row, a recovered parent not adopting
    its waiting subtree), which is why the complete check is the cheap
    one and is worth having.

    NULL rows are excluded on both sides: NULL means "not yet computed",
    readers fall back to the recursive walk, and a partially-filled
    corpus is correct rather than broken.

    Cycles do not produce false positives. `break_cycle` self-roots one
    member and propagation then carries that root around the loop, so a
    converged cycle is coherent even though it disagrees with
    `find_thread_root` (which is why the sampling verifier has to treat
    cycles specially and this one does not).

    Nor do self-parents, of which production had 1,360 when last
    measured. `p.id != a.id` is belt-and-braces there rather than
    load-bearing: a self-referential `In-Reply-To` resolves `p` to `a`
    and therefore `pal` to `al`, so the comparison is a row against
    itself and is false whatever the root is. The guard is kept for
    symmetry with `seed_roots` and `_set_subtree_root`, where the same
    condition IS load-bearing. Removing it is a deliberately equivalent
    mutation; do not go looking for the test that catches it.
    """
    rows = session.execute(
        text(
            """
            SELECT al.article_id, a.message_id,
                   al.thread_root_id AS own_root,
                   pal.thread_root_id AS parent_root
              FROM article_lists al
              JOIN articles a ON a.id = al.article_id
              JOIN articles p ON p.message_id = a.thread_parent
              JOIN article_lists pal
                     ON pal.article_id = p.id
                    AND pal.inbox_id = al.inbox_id
             WHERE al.inbox_id = :ix
               AND p.id != a.id
               AND al.thread_root_id IS NOT NULL
               AND pal.thread_root_id IS NOT NULL
               AND al.thread_root_id != pal.thread_root_id
             LIMIT :lim
            """
        ),
        {"ix": inbox.id, "lim": limit},
    ).all()
    return [
        {
            "article_id": article_id,
            "message_id": message_id,
            "stored_root": own_root,
            "expected_root": parent_root,
            "note": "disagrees with its parent's root; thread is split",
        }
        for article_id, message_id, own_root, parent_root in rows
    ]


def verify_thread_roots(session: Session, inbox, limit: int = 200) -> list[dict]:
    """Recompute roots for a sample and report disagreements.

    The failure this exists for is invisible: a maintenance bug splits
    a conversation in two, both halves render fine, and nothing errors.
    Only recomputing catches it.

    Cyclic threads are excluded rather than reported, because the
    column and `find_thread_root` disagree there by design (see
    `break_cycle`). A row is treated as cyclic when walking up from it
    re-enters a message already seen.
    """
    from sqlalchemy.orm import aliased

    from mimir.models import Article, ArticleList
    from mimir.threading import _find_thread_root_cte

    # RANDOM, not newest-first. `ORDER BY id DESC` samples only the
    # most recent rows, which on this corpus is roughly the last hour
    # of ingest, so a daily run would structurally never see corruption
    # written during a deploy window, i.e. exactly the damage worth
    # detecting.
    rows = session.execute(
        select(Article.id, Article.message_id, ArticleList.thread_root_id)
        .join(ArticleList, ArticleList.article_id == Article.id)
        .where(
            ArticleList.inbox_id == inbox.id,
            ArticleList.thread_root_id.is_not(None),
        )
        .order_by(func.random())
        .limit(limit)
    ).all()

    def _is_cyclic(mid: str) -> bool:
        """Walk up from one message, bounded, scoped to this inbox.

        Deliberately NOT a whole-corpus parent map: that materialised
        every article in the database (~2.4 GB resident at production
        scale) to answer a question about at most `limit` rows, in a
        CLI process that may be memory-capped. It was also GLOBAL, so a
        single sender-controlled cycle anywhere would mark every
        downstream message cyclic and silently exempt it from
        verification, which is a false-negative path in the one
        detector this failure mode has.
        """
        seen = {mid}
        cur = mid
        for _ in range(MAX_CYCLE_WALK):
            parent = session.scalar(
                select(Article.thread_parent)
                .join(ArticleList, ArticleList.article_id == Article.id)
                .where(
                    Article.message_id == cur,
                    ArticleList.inbox_id == inbox.id,
                )
            )
            if parent is None:
                return False
            if parent in seen:
                return True
            seen.add(parent)
            cur = parent
        return True

    def _parent_root(mid: str):
        """The stored root of this row's in-inbox parent, if any."""
        return session.execute(
            select(ArticleList.thread_root_id)
            .join(Article, Article.id == ArticleList.article_id)
            .join(
                _Child := aliased(Article),
                _Child.thread_parent == Article.message_id,
            )
            .where(_Child.message_id == mid, ArticleList.inbox_id == inbox.id)
        ).scalar()

    mismatches: list[dict] = []
    for art_id, mid, stored_root in rows:
        if _is_cyclic(mid):
            # Downstream of a cycle is NOT itself a cycle member, and
            # skipping it outright let one crafted `In-Reply-To` loop
            # exempt every message that ever replied into it, in the
            # only detector this failure mode has. The CTE genuinely
            # disagrees inside the loop, so full agreement can't be
            # demanded, but COHERENCE can: a row must share its
            # parent's root.
            parent_root = _parent_root(mid)
            if parent_root is not None and stored_root != parent_root:
                mismatches.append(
                    {
                        "article_id": art_id,
                        "message_id": mid,
                        "stored_root": stored_root,
                        "expected_root": parent_root,
                        "note": "downstream of a cycle; checked for coherence",
                    }
                )
            continue
        expected_mid = _find_thread_root_cte(session, inbox, mid) or mid
        expected_id = session.scalar(
            select(Article.id).where(Article.message_id == expected_mid)
        )
        if expected_id is not None and stored_root != expected_id:
            mismatches.append(
                {
                    "article_id": art_id,
                    "message_id": mid,
                    "stored_root": stored_root,
                    "expected_root": expected_id,
                }
            )
    return mismatches
