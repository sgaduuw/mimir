"""Side-effect imports of route submodules.

Each module attaches route handlers to `bp_web` at import time;
the package `__init__.py` imports this subpackage to trigger the
registration before `create_app` registers the blueprint.
"""

from mimir.web.routes import (
    api,  # noqa: F401
    attachments,  # noqa: F401
    dashboards,  # noqa: F401
    feeds,  # noqa: F401
    health,  # noqa: F401
    maintainers,  # noqa: F401
    message_id,  # noqa: F401
    search,  # noqa: F401
    series_diff,  # noqa: F401
    sitemaps,  # noqa: F401
    static_meta,  # noqa: F401
    timeviews,  # noqa: F401
)
from mimir.web.routes import message as message_route  # noqa: F401
from mimir.web.routes import thread as thread_route  # noqa: F401
