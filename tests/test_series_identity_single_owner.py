"""Series identity is compared in one place.

A series is (patch_series_key, COALESCE(patch_series_head_id, 0)) since
#816. A reader comparing the key alone treats every series under a
reused cover title as one, which is the bug #816 fixes, and it looks
correct until a re-chain writes the first head. So bare key
equality may appear only inside the helpers in `mimir/patch_series.py`
(AGENTS.md: whoever builds an identifier and whoever accepts it share
one predicate).

Scanned with `ast`, not grep: keyword arguments (`patch_series_key=x`)
and plain assignments are not comparisons and must not trip it.
Not covered: `tuple_(...)` membership and Python-side tuple keys, which
the folding readers build from `series_head_sql` / `series_head`.
"""

import ast
import re
from pathlib import Path

import mimir

ROOT = Path(mimir.__file__).parent
HELPERS = {
    ("patch_series.py", "same_series_sql"),
    ("patch_series.py", "same_series"),
    # Reads every series of a key, to cut it into series.
    ("patch_series_chains.py", "_load_key"),
}
# SQL text: `x.patch_series_key = ...` or `... = x.patch_series_key`.
SQL_EQ = re.compile(r"patch_series_key\s*=|=\s*[\w{}.]*patch_series_key")


def _is_key(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "patch_series_key"


def _text(node: ast.AST) -> str | None:
    """A string literal's text; an f-string's with `{}` per field."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_text(v) or "{}" for v in node.values)
    return None


def _hits(path: Path):
    """(function, line) of every key comparison in `path`."""
    tree = ast.parse(path.read_text(), filename=str(path))
    docstrings = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef)
        and n.body
        and isinstance(n.body[0], ast.Expr)
    }
    # An f-string's literal parts are read through the f-string.
    docstrings |= {
        id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values
    }
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for n in ast.walk(fn):
                owner[id(n)] = fn.name  # innermost wins: walk is outer-first
    for n in ast.walk(tree):
        if isinstance(n, ast.Compare) and any(
            isinstance(op, ast.Eq | ast.NotEq) for op in n.ops
        ):
            if any(_is_key(x) for x in (n.left, *n.comparators)):
                yield owner.get(id(n)), n.lineno
        elif isinstance(n, ast.Constant | ast.JoinedStr) and id(n) not in docstrings:
            text = _text(n)
            if text and SQL_EQ.search(text):
                yield owner.get(id(n)), n.lineno


def test_series_key_is_compared_only_inside_the_helpers():
    allowed, stray = [], []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for fn, line in _hits(path):
            (allowed if (rel, fn) in HELPERS else stray).append(f"{rel}:{line} {fn}")
    # Precondition: the scan sees the helpers' own spelling (one SQL,
    # one ORM, and the two key reads in `_load_key`), so it cannot
    # pass by seeing nothing.
    assert len(allowed) == 4, allowed
    assert stray == []
