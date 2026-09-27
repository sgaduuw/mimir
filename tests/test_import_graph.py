"""Import-graph invariants for the `mimir.seo` <-> `mimir.web` pair.

`mimir/seo/__init__.py` states the rule in its own docstring: the few
JSON-LD / Atom helpers that need `mimir.web`'s display filters import
them INSIDE function bodies, "to avoid an import-time cycle (web
imports sitemap/JSON-LD builders from this package at module load)".

Nothing enforced it, so a module-level `from mimir.web.urls import ...`
could be added to `mimir/seo/sitemaps.py` without any test noticing.
Whether the cycle actually fires depends only on which of the two
packages an import chain reaches first, and today that ordering is an
accident of the line order in `mimir/__init__.py` rather than anything
structural.
"""

import ast
import pathlib
import subprocess
import sys
import textwrap

MIMIR = pathlib.Path(__file__).resolve().parent.parent / "mimir"


def test_seo_reaching_mimir_web_first_does_not_raise_importerror():
    """`mimir.seo` must import cleanly when it is reached BEFORE
    `mimir.web`.

    Property pinned: the two packages must not form an import-time
    cycle, in either direction, regardless of which one an import chain
    happens to touch first.

    Why the current code violates it: `mimir/seo/sitemaps.py` imports
    `mimir.web.urls` at module level. Importing `mimir.seo` first runs
    `mimir/seo/__init__.py` down to `from mimir.seo.sitemaps import
    (...)`, which pulls in `mimir.web`, whose `__init__` imports its
    routes, one of which does `from mimir.seo import inbox_sitemap_xml`.
    At that moment `mimir.seo` is still executing its own sitemaps
    import, so that name is not bound yet:

        ImportError: cannot import name 'inbox_sitemap_xml' from
        partially initialized module 'mimir.seo' (most likely due to a
        circular import)

    It does not fire on any entry point today only because
    `mimir/__init__.py` imports `mimir.cli` and then `mimir.web` before
    anything reaches `mimir.seo`, and nothing under `mimir/cli/` imports
    `mimir.seo` at module level. One new module-level `import mimir.seo`
    anywhere in that chain, or a reorder of those two lines, takes the
    whole application down at import time.

    The subprocess installs a stub `mimir` package (correct `__path__`,
    no body) so that `mimir/__init__.py`'s incidental ordering is out of
    the picture and only the `seo` <-> `web` relationship is under test.
    A subprocess rather than an in-process stub so the rest of the suite
    keeps the real `sys.modules`.
    """
    program = textwrap.dedent(
        f"""
        import sys, types
        pkg = types.ModuleType("mimir")
        pkg.__path__ = [{str(MIMIR)!r}]
        # A few modules read `mimir.__version__` at import time.
        pkg.__version__ = "0.0.0+test"
        sys.modules["mimir"] = pkg
        import mimir.seo
        print("OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "importing mimir.seo before mimir.web raised:\n"
        f"{proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else proc.stderr}"
    )


def test_mimir_web_is_entered_before_mimir_seo():
    """`mimir.web`'s own submodules must start executing BEFORE
    `mimir.seo` does.

    This is the ordering half of the rule the two tests around it cover
    structurally. `mimir/seo/__init__.py` says its `mimir.web` imports
    live inside function bodies "to avoid an import-time cycle", and
    `mimir/web/__init__.py`'s `# isort: off` block says keeping the
    `mimir.seo` import below the side-effect imports is what preserves
    that, because the one-way relationship "only holds while `mimir.web`
    is the package entered first".

    The file does not do that. `from mimir.seo import (...)` sits ABOVE
    the fence, so `mimir.seo` is entered first:

        develop : _blueprint, filters, seo,        routes, hooks, errors
        HEAD    : seo,        _blueprint, filters, routes, hooks, errors

    Inert today only because every `mimir.web` import under `mimir/seo/`
    is function-local. Add one module-level one and the difference is an
    outage, not a nit: with a `from mimir.web import bp_web` at the top
    of `mimir/seo/atom.py`, `import mimir.web` raises

        ImportError: cannot import name 'bp_web' from partially
        initialized module 'mimir.web'

    on HEAD's order and succeeds on develop's.

    `sys.modules` is insertion-ordered and a module is registered there
    before its body runs, so its key order is the module-entry order.
    Asserted in a subprocess because the suite's own imports have already
    populated `sys.modules` by the time any test runs.
    """
    program = textwrap.dedent(
        """
        import sys
        import mimir.web  # noqa: F401
        order = list(sys.modules)
        missing = [
            n for n in ("mimir.seo", "mimir.web._blueprint")
            if n not in sys.modules
        ]
        # Without this, a rename of either module would make the
        # comparison below vacuous rather than red.
        print("MISSING:" + ",".join(missing))
        print("BLUEPRINT:%d" % (order.index("mimir.web._blueprint")
                                if not missing else -1))
        print("SEO:%d" % (order.index("mimir.seo") if not missing else -1))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stderr}"
    out = dict(
        line.split(":", 1) for line in proc.stdout.strip().splitlines() if ":" in line
    )
    assert out["MISSING"] == "", f"probe could not locate: {out['MISSING']}"
    blueprint_at, seo_at = int(out["BLUEPRINT"]), int(out["SEO"])
    assert blueprint_at < seo_at, (
        "mimir.seo is entered before mimir.web._blueprint "
        f"(seo at {seo_at}, _blueprint at {blueprint_at}). "
        "The `# isort: off` block in mimir/web/__init__.py claims the "
        "mimir.seo import is kept BELOW the side-effect imports to "
        "preserve the one-way seo -> web relationship; it is above them."
    )


def test_seo_does_not_import_mimir_web_at_module_level():
    """The structural half of the same rule, stated where a reader of
    `mimir/seo/` will meet it.

    The runtime test above only fails while the cycle is closeable. This
    one fails as soon as the convention is broken, which is the point at
    which it is cheap to fix, and it names the offending file.
    """
    offenders = []
    for path in sorted((MIMIR / "seo").glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        # `tree.body` only, deliberately: `ast.walk` would also reach the
        # in-function imports, which are the sanctioned form.
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "mimir.web"
            ):
                offenders.append(f"{path.name}: from {node.module} import ...")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("mimir.web"):
                        offenders.append(f"{path.name}: import {alias.name}")
    assert not offenders, (
        "mimir/seo/*.py imports mimir.web at module level, which closes "
        "the import cycle its own package docstring says these imports "
        "are kept inside function bodies to avoid:\n  " + "\n  ".join(offenders)
    )


def test_no_route_depends_on_registration_order():
    """Route matching must not depend on the order routes were imported.

    `mimir/web/routes/__init__.py` registers by side-effect import, and
    enabling `I001` reordered that list (`message` moved from position 7
    to 13, `thread` 13 to 14). Werkzeug 3.1.8 does not give a total
    order: `StateMachineMatcher.update()` sorts dynamic transitions by
    `weight` with `list.sort`, which is STABLE, so equal weights fall
    back to insertion order; and a state's `rules` list is appended to
    and never sorted, so a terminal state holding two rules returns the
    first one whose method matches. Registration order is a real
    tiebreaker.

    Today nothing ties, so the reshuffle was inert. That is luck rather
    than construction, which is exactly what wants pinning: the moment a
    new route ties, `routes/__init__.py`'s import order becomes
    load-bearing with no fence and no warning, and a passing suite will
    not say so.
    """
    from collections import Counter

    from mimir import create_app

    app = create_app()
    matcher = app.url_map._matcher

    visited = 0
    ties: list[tuple[str, int, int]] = []
    multi: list[tuple[str, list[str]]] = []

    def walk(state, path="/"):
        nonlocal visited
        visited += 1
        weights = Counter()
        for part, nxt in state.dynamic:
            # `Weighting` is a NamedTuple whose members include
            # lists, so it is unhashable; compare by its repr.
            weights[repr(part.weight)] += 1
            walk(nxt, path + "<>/")
        for w, n in weights.items():
            if n > 1:
                ties.append((path, w, n))
        if len(state.rules) > 1:
            multi.append((path, [str(r) for r in state.rules]))
        for part, nxt in state.static.items():
            walk(nxt, path + part + "/")

    walk(matcher._root)

    # Both assertions below are absence claims, so a walk that visited
    # nothing would satisfy them. Pin that it actually traversed.
    assert visited > 20, (
        f"the matcher walk visited {visited} states for "
        f"{len(list(app.url_map.iter_rules()))} rules; the assertions "
        "below are absence claims and would pass vacuously"
    )

    assert not ties, (
        f"dynamic transitions share a weight, so Werkzeug's stable sort "
        f"falls back to REGISTRATION order and route imports become "
        f"load-bearing: {ties}"
    )
    assert not multi, (
        f"a terminal state holds more than one rule, so the first "
        f"method-matching registration wins: {multi}"
    )
