"""Time the pull-request classification of closed releases on production
data, and compare the merged count with Linus's merges (mimir#1135).
Writes nothing: it reads the release's cached membership and calls
`calculate_pulls`, never `warm_release_pulls`, so no cache row is set.

Needs the #1135 code (`release_history.calculate_pulls`). Run in the
tasks container (it has the database, the mirrors and the broker client):

    podman exec -i "$MIMIR_TASKS" python - < tools/measure_release_pulls.py

Optional: TAGS=<comma-separated release tags> (default: the two newest
closed releases, which #1135's acceptance checks against `git log`),
RUNS=<n> (default 3).

Prints, per release: the cached membership size, then per run the seconds
and SQL statements of `calculate_pulls`, the state counts and the summary
the coverage note reads, and the merges on Linus's first-parent line in
vPREV..vN whose subject names a remote ("Merge tag 'x' of git://...",
"Merge branch 'y' of ..."), the manual check for the merged count. The
two need not match exactly: one pull can be merged twice, and a merge can
come from a pull that was not sent to the list. Write the figures, the
date and what was counted next to `PULLS_FILL_BUDGET_SEC`.
"""

import os
import re
import subprocess
import time
from collections import Counter

from sqlalchemy import event

from mimir import release_history as history
from mimir.extensions import SessionLocal

RUNS = int(os.environ.get("RUNS", "3"))
catalog = history.release_catalog()
tags = os.environ.get("TAGS")
releases = [r for r in catalog if r.tag in tags.split(",")] if tags else catalog[:2]
if not releases:
    raise SystemExit("no matching release in the catalog")
PULL_MERGE = re.compile(r"^Merge (?:tag|branch) .+ of \S+")

for release in releases:
    cached = history._cached_git(release)
    if cached is None or cached[1] is None:
        print(f"{release.tag}: no cached membership; wait for the warm")
        continue
    members = cached[1]
    previous = next((r for r in catalog if r.tag == release.previous_tag), None)
    previous_git = history._cached_git(previous) if previous else None
    if release.previous_tag and previous_git is None:
        print(f"{release.tag}: {release.previous_tag} has no cached membership")
        continue
    previous_members = previous_git[1] if previous_git else None
    print(
        f"{release.tag} ({release.previous_tag}..{release.tag}): {len(members)} commits"
    )
    with SessionLocal() as s:
        statements = []
        event.listen(
            s.get_bind(), "before_cursor_execute", lambda *a: statements.append(1)
        )
        result = None
        for run in range(1, RUNS + 1):
            statements.clear()
            t0 = time.perf_counter()
            result = history.calculate_pulls(s, release, members, previous_members)
            secs = time.perf_counter() - t0
            print(
                f"  run {run}: calculate_pulls {secs:.3f}s, {len(statements)} statements"
            )
            s.rollback()
    if result is None:
        print("  a tag date is missing; nothing classified")
        continue
    summary, rows = result
    print(f"  states: {dict(Counter(state for _, state in rows))}")
    print(f"  summary: {summary.model_dump()}")
    subjects = subprocess.run(
        [
            "git",
            "-C",
            str(release.path),
            "log",
            "--merges",
            "--first-parent",
            "--format=%s",
            f"{release.previous_oid}..{release.oid}",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.splitlines()
    pulled = sum(bool(PULL_MERGE.match(line)) for line in subjects)
    print(f"  git log: {len(subjects)} first-parent merges, {pulled} name a remote")
