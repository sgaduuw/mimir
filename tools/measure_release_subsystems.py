"""Time the subsystem grouping of one release's recorded references on
production data (mimir#1136, step 3). Writes no release cache; may fill
the shared rules snapshot through the broker, as any subsystem page does.

Run in the tasks container (it has the database and the broker client):

    podman exec -i "$MIMIR_TASKS" python - < tools/measure_release_subsystems.py

Optional: TAG=<release tag> (default v7.3, the largest release on
2026-11-08), RUNS=<n> (default 3).

It reads the release's cached ID list (`:links_data`) and never
calculates or writes a release cache: a release with no cached list is
reported and skipped. Grouping may fill the process-local rules index
and, if absent, the shared rules-snapshot cache row that every subsystem
page already reads and fills; the first run pays for that, which is why
runs are reported one by one.

Prints, for the release: the IDs read, how many are archived references
(the count the page shows), the postings and sections they make, then per
run the seconds and SQL statements of `calculate_subsystems`, split into
the archived-ID query and `rank_postings`, and for comparison one
`archived_page` page 1 (what the page already pays per request). Write
the run's figures, its date and what was counted next to
`calculate_subsystems` (the 2026-11-09 figures are recorded there).
"""

import os
import time
from itertools import batched

from sqlalchemy import event

from mimir import cache
from mimir import release_history as history
from mimir.extensions import SessionLocal
from mimir.merge_window import rank_postings

TAG = os.environ.get("TAG", "v7.3")
RUNS = int(os.environ.get("RUNS", "3"))

release = next((r for r in history.release_catalog() if r.tag == TAG), None)
if release is None:
    raise SystemExit(f"no release {TAG} in the catalog")
payload = cache.get(release.key + ":links_data")
if payload is None:
    raise SystemExit(f"{TAG}: no cached ID list; open its page or wait for the warm")
ids = payload["message_ids"]
print(f"{TAG}: {len(ids)} cached message IDs (distinct: {len(set(ids))})")

with SessionLocal() as s:
    statements = []
    event.listen(s.get_bind(), "before_cursor_execute", lambda *a: statements.append(1))

    def timed(fn):
        statements.clear()
        t0 = time.perf_counter()
        result = fn()
        return result, time.perf_counter() - t0, len(statements)

    archived = []

    def archived_ids():
        for chunk in batched(set(ids), 900):
            archived.extend(
                s.execute(history._ARCHIVED_SERIES_SQL, {"ids": chunk}).all()
            )

    _, secs, n = timed(archived_ids)
    print(
        f"archived references: {len(archived)} (ID query {secs:.3f}s, {n} statements)"
    )
    (postings, _), secs, n = timed(lambda: rank_postings(s, archived))
    print(
        f"postings: {len(postings)} (first rank_postings {secs:.3f}s, {n} statements)"
    )
    result = None
    for run in range(1, RUNS + 1):
        result, secs, n = timed(lambda: history.calculate_subsystems(s, ids))
        print(f"run {run}: calculate_subsystems {secs:.3f}s, {n} statements")
    print(
        f"sections: {len(result.sections)}, smaller subsystems: "
        f"{len(result.smaller)} holding {result.smaller_patches}, "
        f"references counted: {result.references}"
    )
    (_, total), secs, n = timed(lambda: history.archived_page(s, ids, 1))
    print(
        f"for comparison, archived_page page 1: {secs:.3f}s, {n} statements, total {total}"
    )
