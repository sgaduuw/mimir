"""Measure grouping /merge-window by ONE primary subsystem per patch,
on production data (mimir#703, grouping question). Read-only.

Run in the tasks container:

    podman exec -i "$MIMIR_TASKS" python - < tools/measure_primary_subsystem.py

Optional env: SAMPLE=<n> (default 300) patches cross-checked against
mimir's own `subsystems_for_article`, to prove the fast matcher gives
the same answers.

Primary subsystem: of the subsystems a patch's files match (F: minus
X:, exactly as `subsystems_for_article`), the one whose matching F:
rule has the longest fixed prefix, i.e. the most specific. THE REST
(the MAINTAINERS catch-all) is never primary; a patch only it matches
lands in "(other)".

Writes nothing except, possibly, the shared rules-snapshot cache row
the subsystem pages already fill (only during the cross-check).

Results on 2026-10-12 are recorded on mimir#703; the matcher is the
basis of mimir#705.

Measured against 3.16.0 code. From 3.17.0 on, `subsystems_for_article`
uses the indexed matcher itself (#705), so a rerun no longer reproduces
the 2026-10-12 baselines: the cross-check then compares this matcher
with the same algorithm, and agreement proves nothing.
"""

import collections
import os
import statistics
import time

from sqlalchemy import select, text

from mimir.extensions import SessionLocal
from mimir.models import Article, ArticleFile, Inbox, Subsystem, SubsystemPath
from mimir.subsystems import path_matches_glob, subsystems_for_article

SAMPLE = int(os.environ.get("SAMPLE", "300"))
CATCH_ALL = "THE REST"

QUEUED = text("""
SELECT a.id AS article_id, a.canonical_inbox_id
  FROM (SELECT DISTINCT n.message_id FROM mainline_commits n
         WHERE n.tree_name = 'linux-next'
           AND NOT EXISTS (SELECT 1 FROM mainline_commits l
                            WHERE l.tree_name = 'linus'
                              AND l.message_id = n.message_id)) q
  JOIN articles a ON a.message_id = q.message_id
""")


def fixed_prefix(glob):
    """The glob up to its first wildcard: what every match starts with."""
    cut = min((i for i in (glob.find(c) for c in "*?[") if i != -1), default=len(glob))
    return glob[:cut]


def dir_key(prefix):
    return prefix[: prefix.rfind("/") + 1]


def path_keys(path):
    """Every directory a rule for this path can be filed under, plus
    the path as a directory (a `dir/` rule also matches `dir` itself)."""
    keys = [""]
    for i, ch in enumerate(path):
        if ch == "/":
            keys.append(path[: i + 1])
    keys.append(path + "/")
    return keys


def section(title):
    print(f"\n== {title}")


with SessionLocal() as s:
    names = dict(s.execute(select(Subsystem.id, Subsystem.name)).all())
    inbox_names = dict(s.execute(select(Inbox.id, Inbox.name)).all())

    t0 = time.perf_counter()
    index = collections.defaultdict(list)  # dir key -> [(sub_id, glob, specificity)]
    excludes = collections.defaultdict(list)  # sub_id -> [glob]
    for sub_id, glob, is_exclude in s.execute(
        select(SubsystemPath.subsystem_id, SubsystemPath.glob, SubsystemPath.is_exclude)
    ).all():
        if is_exclude:
            excludes[sub_id].append(glob)
        else:
            prefix = fixed_prefix(glob)
            index[dir_key(prefix)].append((sub_id, glob, len(prefix)))
    t_index = time.perf_counter() - t0

    def match(paths):
        """{sub_id: best specificity} for a patch's files."""
        best = {}
        for path in paths:
            for key in path_keys(path):
                for sub_id, glob, spec in index.get(key, ()):
                    if spec <= best.get(sub_id, -1):
                        continue
                    if path_matches_glob(path, glob) and not any(
                        path_matches_glob(path, x) for x in excludes.get(sub_id, ())
                    ):
                        best[sub_id] = spec
        return best

    queued = s.execute(QUEUED).all()
    ids = [r.article_id for r in queued]
    canon = {r.article_id: r.canonical_inbox_id for r in queued}
    files = collections.defaultdict(list)
    for i in range(0, len(ids), 900):
        for aid, path in s.execute(
            select(ArticleFile.article_id, ArticleFile.path).where(
                ArticleFile.article_id.in_(ids[i : i + 900])
            )
        ).all():
            files[aid].append(path)

    section("Input")
    print(f"queued archived patches: {len(ids)}; with touched files: {len(files)}")
    print(
        f"rules: {sum(len(v) for v in index.values())} F:, "
        f"{sum(len(v) for v in excludes.values())} X:, in {len(index)} index buckets "
        f"(built in {t_index * 1000:.0f} ms); rules filed under '' (tested for every file): "
        f"{len(index.get('', []))}"
    )

    t0 = time.perf_counter()
    matches = {aid: match(paths) for aid, paths in files.items()}
    t_match = time.perf_counter() - t0

    section("Speed (fast matcher)")
    print(
        f"all {len(matches)} patches in {t_match:.2f} s "
        f"({t_match / max(len(matches), 1) * 1000:.2f} ms per patch)"
    )

    section(f"Cross-check against subsystems_for_article ({SAMPLE} patches)")
    mismatches = []
    for aid in sorted(matches)[:SAMPLE]:
        ours = {names[sid] for sid in matches[aid]}
        theirs = {h.name for h in subsystems_for_article(s, s.get(Article, aid))}
        if ours != theirs:
            mismatches.append((aid, sorted(ours ^ theirs)[:5]))
    print(f"mismatches: {len(mismatches)} of {min(SAMPLE, len(matches))}")
    for aid, diff in mismatches[:5]:
        print(f"  article {aid}: differs on {diff}")

    rest_ids = {sid for sid, n in names.items() if n == CATCH_ALL}
    primary = {}
    per_patch = []
    for aid, best in matches.items():
        real = {sid: spec for sid, spec in best.items() if sid not in rest_ids}
        per_patch.append(len(real))
        if not real:
            primary[aid] = "(other)"
            continue
        top = max(real.values())
        primary[aid] = min(names[sid] for sid, spec in real.items() if spec == top)

    section(f"Overlap, {CATCH_ALL} excluded")
    print(
        f"patches in more than one subsystem: {sum(1 for n in per_patch if n > 1)} of {len(per_patch)}; "
        f"median subsystems per patch: {statistics.median(per_patch) if per_patch else 0}"
    )

    groups = collections.Counter(primary.values())
    total = len(primary)
    section("Primary subsystem groups")
    print(
        f"groups: {len(groups)} (incl. '(other)': {groups.get('(other)', 0)} patches)"
    )
    print("min patches | groups that size or bigger | patches they cover")
    for n in (1, 3, 5, 10, 25, 50, 100):
        big = [c for name, c in groups.items() if c >= n and name != "(other)"]
        print(f"  >= {n:3d} | {len(big):5d} | {sum(big):6d} ({sum(big) / total:.0%})")
    print("largest 40:")
    for name, c in groups.most_common(40):
        print(f"  {c:6d}  {name}")

    section("Largest list, split by primary subsystem")
    by_list = collections.Counter(canon[aid] for aid in primary)
    biggest, size = by_list.most_common(1)[0]
    within = collections.Counter(
        primary[aid] for aid in primary if canon[aid] == biggest
    )
    print(
        f"{inbox_names.get(biggest, biggest)}: {size} patches with files, "
        f"{len(within)} primary groups; groups >= 10 patches: {sum(1 for c in within.values() if c >= 10)}"
    )
    for name, c in within.most_common(15):
        print(f"  {c:6d}  {name}")
