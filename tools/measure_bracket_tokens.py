"""Measure the options for recognising patch series whose subject
bracket has no PATCH token (mimir#711). Read-only.

Run in the tasks container:

    podman exec -i "$MIMIR_TASKS" python - < tools/measure_bracket_tokens.py

Two index range scans over `subject_normalized` (subjects starting with
"["), then the stored subject itself is unfolded (#710) and parsed:

- A: numbered brackets (`N/M`) with no PATCH anywhere in the subject,
  grouped by their wording with numbers and versions blanked, so
  `[net-next v7 02/10]` and `[net-next v8 01/3]` count together. Option
  1 (a tree tag) and option 2 (anything numbered, minus known non-patch
  words) are both read off this table.
- B: a first bracket without PATCH followed by a `[PATCH ... N/M]`
  bracket (option 3, `[OE-core] [PATCH 3/7]`), grouped by the first
  bracket.
"""

import collections
import re
import time

from sqlalchemy import text

from mimir.extensions import SessionLocal
from mimir.parser import unfold

BRACKET = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
N_OF_M = re.compile(r"\b0*\d+\s*/\s*\d+\b")
VERSION = re.compile(r"\bv\d+\b", re.IGNORECASE)
PATCH = re.compile(r"\bPATCH\b", re.IGNORECASE)
NON_PATCH = ("GIT PULL", "PULL", "ANNOUNCE")

ROWS_A = text("""
SELECT a.id, a.subject, a.patch_series_position FROM articles a
 WHERE a.subject_normalized >= '[' AND a.subject_normalized < '\\'
   AND instr(a.subject_normalized, 'patch') = 0
""")
ROWS_B = text("""
SELECT a.id, a.subject, a.patch_series_position FROM articles a
 WHERE a.subject_normalized >= '[' AND a.subject_normalized < '\\'
   AND instr(a.subject_normalized, '] [patch') > 0
""")


def signature(inside):
    sig = N_OF_M.sub("N/M", inside)
    sig = VERSION.sub("vN", sig)
    return " ".join(sig.split())


def section(title):
    print(f"\n== {title}")


with SessionLocal() as s:
    t0 = time.perf_counter()
    rows_a = s.execute(ROWS_A).all()
    t_a = time.perf_counter() - t0
    sigs = collections.Counter()
    sample = {}
    total_numbered = positioned = 0
    for r in rows_a:
        subject = unfold(r.subject) or ""
        m = BRACKET.match(subject.strip())
        if m is None or not N_OF_M.search(m.group(1)):
            continue
        total_numbered += 1
        positioned += r.patch_series_position is not None
        sig = signature(m.group(1))
        sigs[sig] += 1
        sample.setdefault(sig, subject[:110])

    def non_patch(sig):
        upper = sig.upper()
        return any(word in upper for word in NON_PATCH)

    section("A: numbered bracket, no PATCH (options 1 and 2)")
    print(f"scanned {len(rows_a)} bracketed subjects without 'patch' in {t_a:.1f} s")
    print(f"numbered (N/M in the first bracket): {total_numbered}; "
          f"already with a series position: {positioned}")
    print(f"option 2 catch (numbered, minus {', '.join(NON_PATCH)}): "
          f"{sum(c for sig, c in sigs.items() if not non_patch(sig))}")
    print(f"distinct wordings: {len(sigs)}; top 50:")
    for sig, c in sigs.most_common(50):
        print(f"  {c:7d}  [{sig}]  e.g. {sample[sig]}")

    t0 = time.perf_counter()
    rows_b = s.execute(ROWS_B).all()
    t_b = time.perf_counter() - t0
    tags = collections.Counter()
    tag_sample = {}
    caught = positioned_b = 0
    for r in rows_b:
        subject = unfold(r.subject) or ""
        m = BRACKET.match(subject.strip())
        if m is None or PATCH.search(m.group(1)):
            continue
        second = BRACKET.match(m.group(2))
        if second is None or not PATCH.search(second.group(1)) or not N_OF_M.search(second.group(1)):
            continue
        caught += 1
        positioned_b += r.patch_series_position is not None
        tags[m.group(1)] += 1
        tag_sample.setdefault(m.group(1), subject[:110])

    section("B: leading list tag, then [PATCH ... N/M] (option 3)")
    print(f"scanned {len(rows_b)} subjects with '] [patch' in {t_b:.1f} s")
    print(f"caught: {caught}; already with a series position: {positioned_b}")
    print(f"distinct leading tags: {len(tags)}; top 25:")
    for tag, c in tags.most_common(25):
        print(f"  {c:7d}  [{tag}]  e.g. {tag_sample[tag]}")
