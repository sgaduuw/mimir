"""Independent oracle for `thread_window` (#798 review).

Random tree shapes, with dateless messages and date ties, compared against
a Python model built from the parent edges: true depths, the path, the
nearest siblings in `(COALESCE(date, floor), id)` order, the first
children, and that every shown node's replies are exactly shown + gap +
hidden.
"""

import random
from datetime import datetime

import pytest
from sqlalchemy import select

from mimir.extensions import SessionLocal
from mimir.models import Article, Inbox
from mimir.threading import ThreadGap, ThreadNode, thread_window
from tests.test_routes._helpers import seed_thread_shape

FLOOR = datetime(1, 1, 1)


def _key(a):
    return (a.date or FLOOR, a.id)


@pytest.mark.parametrize("seed", range(12))
def test_window_matches_model(tmp_path, seed):
    rng = random.Random(seed)
    n = rng.randint(30, 90)
    # Skewed parents so some nodes get many children and depth exceeds 20.
    edges = [("m0", None)]
    for i in range(1, n):
        if rng.random() < (0.45 if seed < 6 else 0.8):
            p = i - 1  # chain step
        elif rng.random() < 0.5:
            p = rng.choice([0, 1, 2][:i])  # fan
        else:
            p = rng.randrange(i)
        edges.append((f"m{i}", f"m{p}"))
    seeded = seed_thread_shape(tmp_path, "alpha", edges)
    with SessionLocal() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        arts = {mid: s.get(Article, seeded[mid][0]) for mid, _ in edges}
        # Dateless and tied dates.
        for mid in rng.sample(list(arts)[1:], 4):
            arts[mid].date = None
        tied = rng.sample(list(arts)[1:], 6)
        for mid in tied[1:]:
            if arts[mid].date is not None and arts[tied[0]].date is not None:
                arts[mid].date = arts[tied[0]].date
        s.commit()

        parent = {mid: p for mid, p in edges}
        children = {mid: [] for mid, _ in edges}
        for mid, p in edges:
            if p:
                children[p].append(mid)

        def depth(mid):
            d = 0
            while parent[mid]:
                mid = parent[mid]
                d += 1
            return d

        root = arts["m0"]
        for target in rng.sample(list(arts), 15):
            art = arts[target]
            w = thread_window(s, inbox, root, art)
            assert w is not None
            nodes = [r for r in w.rows if isinstance(r, ThreadNode)]
            by_id = {a.id: m for m, a in arts.items()}
            shown = [by_id[x.id] for x in nodes]
            assert len(shown) == len(set(shown)), "a message shown twice"
            # Depths truthful.
            for x in nodes:
                assert x.depth == depth(by_id[x.id]), (target, by_id[x.id])
            # Path: root and parent shown, path is a prefix chain.
            chain = []
            m = target
            while m:
                chain.append(m)
                m = parent[m]
            chain.reverse()
            assert "m0" in shown
            if parent[target]:
                assert parent[target] in shown
            anc = chain[:-1]
            if len(anc) <= 20:
                assert shown[: len(anc)] == anc
            else:
                assert shown[0] == "m0"
                assert shown[1 : 1 + 19] == anc[-19:]
                gap = w.rows[1]
                assert isinstance(gap, ThreadGap) and gap.gap_count == len(anc) - 20
            # Siblings: nearest 5 either side in key order.
            if parent[target]:
                sibs = sorted(
                    (arts[c] for c in children[parent[target]] if c != target),
                    key=_key,
                )
                before = [a for a in sibs if _key(a) < _key(art)]
                after = [a for a in sibs if _key(a) > _key(art)]
                exp_before = [by_id[a.id] for a in before[-5:]]
                exp_after = [by_id[a.id] for a in after[:5]]
                i = shown.index(target)
                assert shown[i - len(exp_before) : i] == exp_before, target
                kids = sorted((arts[c] for c in children[target]), key=_key)
                exp_kids = [by_id[a.id] for a in kids[:10]]
                assert shown[i + 1 : i + 1 + len(exp_kids)] == exp_kids
                assert shown[i + 1 + len(exp_kids) :] == exp_after
            # Accounting: every shown node's replies are shown, gapped or
            # hidden, exactly.
            gap_for = {}
            depth_t = depth(target)
            for r in w.rows:
                if isinstance(r, ThreadGap):
                    if r.depth == depth_t + 1:
                        gap_for[target] = gap_for.get(target, 0) + r.gap_count
                    elif r.depth == depth_t and parent[target]:
                        gap_for[parent[target]] = (
                            gap_for.get(parent[target], 0) + r.gap_count
                        )
                    elif r.depth == 1 and len(anc) > 20:
                        gap_for["m0"] = gap_for.get("m0", 0) + 1  # path child
            for x in nodes:
                m = by_id[x.id]
                n_shown = sum(1 for c in children[m] if c in shown)
                assert n_shown + gap_for.get(m, 0) + w.hidden_replies.get(
                    x.id, 0
                ) == len(children[m]), (target, m)
            assert w.reply_count == len(children[target])
