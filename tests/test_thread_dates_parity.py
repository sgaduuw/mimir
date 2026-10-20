"""#798 parity: `_thread_dates` (column where materialised, walk otherwise)
gives exactly what the recursive walk alone gives, for every leaf.

The walk SQL is byte-identical to the CTE `_BULK_SQL` carried on base, so
the walk is the pre-change answer. Reuses the enumeration harness from
`tests/test_thread_root_invariants.py`: every acyclic 4-node shape x every
NULL pattern with full membership, then a fixed-seed sample over partial
membership. Dates vary per configuration and include NULLs. A second
inbox carries the same articles, every row pointing at node 0, so a
column read that leaks across inboxes disagrees.
"""

import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import text, update

from mimir.datetime_utils import coerce_dt
from mimir.lifecycle_status import _THREAD_DATES_WALK_SQL, _thread_dates
from mimir.models import Article, Inbox
from tests.test_thread_root_invariants import _N, _apply, _forests, _seed_nodes

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _walk(session, ids, ix_id):
    return {
        r.leaf: coerce_dt(r.last_thread_date)
        for r in session.execute(
            _THREAD_DATES_WALK_SQL, {"ids": ids, "inbox_id": ix_id}
        )
        if r.last_thread_date is not None
    }


def _set_dates(session, ids, rng):
    for art_id in ids:
        date = (
            None if rng.random() < 0.25 else _BASE + timedelta(days=rng.randrange(60))
        )
        session.execute(update(Article).where(Article.id == art_id).values(date=date))


def _other_inbox(session, ids):
    other = Inbox(
        name="parity-other",
        mirror_path="/nonexistent/parity-other",
        upstream_url="https://example.invalid/parity-other",
    )
    session.add(other)
    session.flush()
    for i, art_id in enumerate(ids):
        session.execute(
            text(
                "INSERT INTO article_lists "
                "(article_id, inbox_id, epoch, commit_sha, thread_root_id) "
                "VALUES (:aid, :ix, '0.git', :sha, :root)"
            ),
            {"aid": art_id, "ix": other.id, "sha": f"{i:040d}", "root": ids[0]},
        )
    session.commit()


def _check(session, ix_id, ids, desc):
    got = {k: coerce_dt(v) for k, v in _thread_dates(session, ids, ix_id).items()}
    want = _walk(session, ids, ix_id)
    assert got == want, f"{desc}: got {got} want {want}"


def test_thread_dates_match_the_walk_over_every_shape(session):
    ix, ids = _seed_nodes(session, "parity", _N)
    _other_inbox(session, ids)
    rng = random.Random(798)
    members = set(range(_N))
    configs = 0
    for parent in _forests(_N):
        for null_bits in range(1 << _N):
            nulls = {i for i in range(_N) if null_bits & (1 << i)}
            _apply(session, "parity", ix.id, ids, parent, members, nulls)
            _set_dates(session, ids, rng)
            _check(session, ix.id, ids, f"parent={parent} nulls={sorted(nulls)}")
            configs += 1
    assert configs == 125 * 16


def test_thread_dates_match_the_walk_under_partial_membership(session):
    ix, ids = _seed_nodes(session, "parity", _N)
    _other_inbox(session, ids)
    forests = list(_forests(_N))
    rng = random.Random(20261020)
    for _ in range(1200):
        parent = rng.choice(forests)
        members = {i for i in range(_N) if rng.random() < 0.75}
        if not members:
            continue
        nulls = {i for i in members if rng.random() < 0.5}
        _apply(session, "parity", ix.id, ids, parent, members, nulls)
        _set_dates(session, ids, rng)
        _check(
            session,
            ix.id,
            ids,
            f"parent={parent} members={sorted(members)} nulls={sorted(nulls)}",
        )
