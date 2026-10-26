"""Measure what grouping /merge-window by tree, by mailing list, or by
every matching MAINTAINERS subsystem would look like and cost, on
production data (mimir#703). Read-only.

Run in the tasks container (it has the database and the broker client):

    podman exec -i "$MIMIR_TASKS" python - < tools/measure_merge_window_grouping.py

Optional: BUDGET_SEC=<seconds> (default 600) caps the subsystem timing;
the report says how much it covered rather than extrapolating.

Writes nothing except, possibly, the shared rules-snapshot cache row
that every subsystem page already reads and fills.

Results on 2026-10-12 are recorded on mimir#703.

Measured against 3.16.0 code. From 3.17.0 on, `subsystems_for_article` (now
`subsystems_for_paths`) uses the indexed matcher itself (#705), so a rerun no longer reproduces
the 2026-10-12 baselines: Option B then times the
indexed matcher, not the ~75 ms rule walk it measured then.
"""

import collections
import os
import statistics
import time

from sqlalchemy import func, select, text

from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleFile, SubsystemPath
from mimir.subsystems import article_paths, subsystems_for_paths

BUDGET_SEC = float(os.environ.get("BUDGET_SEC", "600"))
CURATED = sorted(t for t in settings.trees if t not in ("linus", "linux-next"))

# Same definition as /merge-window: linux-next minus Linus, by message-id.
QUEUED = text("""
SELECT q.message_id, a.id AS article_id, a.canonical_inbox_id
  FROM (SELECT DISTINCT n.message_id FROM mainline_commits n
         WHERE n.tree_name = 'linux-next'
           AND NOT EXISTS (SELECT 1 FROM mainline_commits l
                            WHERE l.tree_name = 'linus'
                              AND l.message_id = n.message_id)) q
  LEFT JOIN articles a ON a.message_id = q.message_id
""")

TREES = text("""
SELECT o.message_id, o.tree_name FROM mainline_commits o
 WHERE o.tree_name NOT IN ('linux-next', 'linus')
   AND o.message_id IN (SELECT n.message_id FROM mainline_commits n
                         WHERE n.tree_name = 'linux-next')
""")


def section(title):
    print(f"\n== {title}")


with SessionLocal() as s:
    t0 = time.perf_counter()
    rows = s.execute(QUEUED).all()
    t_query = time.perf_counter() - t0
    archived = [r for r in rows if r.article_id is not None]
    ids = [r.article_id for r in archived]

    with_files = set()
    for i in range(0, len(ids), 900):
        with_files.update(
            s.scalars(
                select(ArticleFile.article_id)
                .where(ArticleFile.article_id.in_(ids[i : i + 900]))
                .distinct()
            )
        )

    section("Queued set")
    print(f"queued message-ids (linux-next minus linus): {len(rows)}")
    print(f"archived here: {len(archived)}; with touched files: {len(with_files)}")
    print(f"queued query: {t_query:.2f} s")
    print(
        f"MAINTAINERS path rules loaded: {s.scalar(select(func.count(SubsystemPath.id)))}"
    )

    section("Option A: by tree")
    trees_by_msgid = collections.defaultdict(set)
    for mid, tree in s.execute(TREES).all():
        trees_by_msgid[mid].add(tree)
    per_tree = collections.Counter()
    only_next = 0
    for r in rows:
        trees = trees_by_msgid.get(r.message_id) or set()
        if not trees:
            only_next += 1
        for t in trees:
            per_tree[t] += 1
    print(f"curated trees: {', '.join(CURATED)}")
    for t, n in per_tree.most_common():
        print(f"  {t}: {n}")
    print(f"  linux-next only (would be 'everything else'): {only_next} of {len(rows)}")

    section("Today: by mailing list (canonical inbox)")
    lists = collections.Counter(r.canonical_inbox_id for r in archived)
    print(
        f"distinct lists: {len(lists)}; "
        f"largest: {lists.most_common(1)[0][1] if lists else 0}"
    )

    section("Option B: by MAINTAINERS subsystem")
    subsystem_count = collections.Counter()
    per_patch = []
    timings = []
    done = 0
    started = time.perf_counter()
    for aid in sorted(with_files):
        if time.perf_counter() - started > BUDGET_SEC:
            break
        article = s.get(Article, aid)
        t1 = time.perf_counter()
        hits = subsystems_for_paths(
            s, [row[2] for row in article_paths(s, article) if row[2]]
        )
        timings.append(time.perf_counter() - t1)
        per_patch.append(len(hits))
        for h in hits:
            subsystem_count[h.name] += 1
        done += 1
    elapsed = time.perf_counter() - started
    print(
        f"resolved {done} of {len(with_files)} patches with files in {elapsed:.1f} s"
        f"{' (stopped at BUDGET_SEC)' if done < len(with_files) else ''}"
    )
    if timings:
        timings.sort()
        print(
            f"per patch: median {statistics.median(timings) * 1000:.1f} ms, "
            f"p95 {timings[int(len(timings) * 0.95)] * 1000:.1f} ms, "
            f"max {timings[-1] * 1000:.1f} ms"
        )
        print(f"distinct subsystems: {len(subsystem_count)}")
        print(f"patches with no subsystem: {sum(1 for n in per_patch if n == 0)}")
        print(
            f"patches in more than one: {sum(1 for n in per_patch if n > 1)}; "
            f"max per patch: {max(per_patch)}"
        )
        print("largest 25 groups:")
        for name, n in subsystem_count.most_common(25):
            print(f"  {n:6d}  {name}")
        sizes = sorted(subsystem_count.values())
        print(
            f"group size: median {statistics.median(sizes)}, "
            f"groups of 1 patch: {sum(1 for n in sizes if n == 1)}"
        )
