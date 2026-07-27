"""Backfill and verification for `article_lists.thread_root_id` (W8).

Ingest maintains the column incrementally (see
`mimir.ingest._pending._resolve_thread_root`); this module fills it in
for rows that predate the column, and proves it right afterwards.

The column is per-inbox because threading is: `find_thread_root` walks
within one inbox, so the same article can root differently in each
inbox it is linked to. Everything here is therefore scoped per inbox.
"""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Safety stop for the propagation loop. Real lkml threads rarely exceed
# ~50 deep and `threading.MAX_DEPTH` caps the read side at 1000, so a
# run that needs more passes than this has hit something pathological
# and should stop rather than spin.
MAX_PASSES = 1200


def _seed_roots(session: Session, inbox_id: int) -> int:
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


def _propagate(session: Session, inbox_id: int) -> int:
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


def _break_cycle(session: Session, inbox_id: int) -> int:
    """Self-root the lowest remaining unrooted article in this inbox.

    Propagation stalls only on a cycle: every member is waiting for a
    parent that is itself waiting. Nothing seeds them, because none has
    an absent parent. Picking the lowest article id and self-rooting it
    lets the next propagation pass carry that root around the loop, so
    the whole cycle converges on one member rather than fragmenting.

    Deliberately NOT what `find_thread_root` returns for a cycle (it
    walks to MAX_DEPTH and lands wherever `1000 mod cycle_length` puts
    it, which is not a root in any useful sense). The disagreement is
    intentional and `verify_thread_roots` excludes cycles rather than
    reporting them as corruption.
    """
    return session.execute(
        text(
            """
            UPDATE article_lists
               SET thread_root_id = article_id
             WHERE inbox_id = :ix
               AND article_id = (
                   SELECT MIN(article_id) FROM article_lists
                    WHERE inbox_id = :ix AND thread_root_id IS NULL
               )
            """
        ),
        {"ix": inbox_id},
    ).rowcount


def backfill_inbox(session: Session, inbox_id: int) -> dict[str, int]:
    """Populate `thread_root_id` for one inbox. Idempotent: only ever
    touches rows that are still NULL, so a re-run after a partial pass
    resumes rather than redoing, and live ingest writing alongside it
    is not clobbered."""
    seeded = _seed_roots(session, inbox_id)
    propagated = 0
    cycles_broken = 0
    for _ in range(MAX_PASSES):
        moved = _propagate(session, inbox_id)
        if moved:
            propagated += moved
            continue
        # Stalled: either done, or only cycles remain.
        if _break_cycle(session, inbox_id):
            cycles_broken += 1
            continue
        break
    else:
        logger.warning(
            "thread-roots: inbox %s hit MAX_PASSES (%s); rows may remain unrooted",
            inbox_id,
            MAX_PASSES,
        )
    return {
        "seeded": seeded,
        "propagated": propagated,
        "cycles_broken": cycles_broken,
    }


def verify_thread_roots(session: Session, inbox, limit: int = 200) -> list[dict]:
    """Recompute roots for a sample and report disagreements.

    The failure this exists for is invisible: a maintenance bug splits
    a conversation in two, both halves render fine, and nothing errors.
    Only recomputing catches it.

    Cyclic threads are excluded rather than reported, because the
    column and `find_thread_root` disagree there by design (see
    `_break_cycle`). A row is treated as cyclic when walking up from it
    re-enters a message already seen.
    """
    from mimir.models import Article, ArticleList
    from mimir.threading import find_thread_root
    from sqlalchemy import select

    rows = session.execute(
        select(Article.id, Article.message_id, ArticleList.thread_root_id)
        .join(ArticleList, ArticleList.article_id == Article.id)
        .where(
            ArticleList.inbox_id == inbox.id,
            ArticleList.thread_root_id.is_not(None),
        )
        .order_by(Article.id.desc())
        .limit(limit)
    ).all()

    parents = dict(
        session.execute(select(Article.message_id, Article.thread_parent)).all()
    )

    def _is_cyclic(mid: str) -> bool:
        seen = {mid}
        cur = parents.get(mid)
        while cur:
            if cur in seen:
                return True
            seen.add(cur)
            cur = parents.get(cur)
        return False

    mismatches: list[dict] = []
    for art_id, mid, stored_root in rows:
        if _is_cyclic(mid):
            continue
        expected_mid = find_thread_root(session, inbox, mid) or mid
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
