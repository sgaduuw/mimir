"""Read-only pre-upgrade inventory, using names exported by the OLD application.

Run from the new checkout with --configured pointing to a JSON array produced
by `json.dump(list(settings.inboxes), ...)` on the deployed old version. The
checker deliberately does not import mimir: new Settings rejects collisions
before an operator can inventory them. --database is the existing SQLite file.
"""

import argparse
import json
import runpy
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configured", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args()
    names = json.loads(args.configured.read_text())
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        parser.error("--configured must contain a JSON array of effective inbox names")
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "mimir" / "_inbox_name.py")
    )
    reserved = namespace["RESERVED_ROOT_WORDS"]
    with sqlite3.connect(
        args.database.resolve().as_uri() + "?mode=ro", uri=True
    ) as conn:
        stored = [
            row[0] for row in conn.execute("SELECT name FROM inboxes ORDER BY name")
        ]
    configured_conflicts = sorted(set(names) & reserved)
    stored_conflicts = sorted(set(stored) & reserved)
    print(
        json.dumps(
            {
                "configured_count": len(names),
                "stored_count": len(stored),
                "configured_conflicts": configured_conflicts,
                "stored_conflicts": stored_conflicts,
            },
            indent=2,
        )
    )
    return int(bool(configured_conflicts or stored_conflicts))


if __name__ == "__main__":
    raise SystemExit(main())
