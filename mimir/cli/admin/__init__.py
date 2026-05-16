"""`admin` — operator surfaces grouped under one Click group.

The submodules attach their commands to the top-level `admin_group`
at import time. The `cli` package `__init__.py` imports each submodule
to trigger the registration, then exposes `admin_group` for
`register_cli` to attach to the Flask app.
"""
import click


@click.group("admin")
def admin_group() -> None:
    """Administrative operations on the underlying data."""


# Side-effect imports: each submodule attaches commands to admin_group
# (or to its own nested group within admin_group) at import time.
from mimir.cli.admin import inbox as _inbox  # noqa: F401, E402
from mimir.cli.admin import failures as _failures  # noqa: F401, E402
from mimir.cli.admin import canonicals as _canonicals  # noqa: F401, E402
