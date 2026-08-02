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
