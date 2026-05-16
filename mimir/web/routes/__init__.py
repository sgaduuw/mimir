"""Side-effect imports of route submodules.

Each module attaches route handlers to `bp_web` at import time;
the package `__init__.py` imports this subpackage to trigger the
registration before `create_app` registers the blueprint.
"""
from mimir.web.routes import api  # noqa: F401
from mimir.web.routes import attachments  # noqa: F401
from mimir.web.routes import dashboards  # noqa: F401
from mimir.web.routes import feeds  # noqa: F401
from mimir.web.routes import health  # noqa: F401
from mimir.web.routes import message as message_route  # noqa: F401
from mimir.web.routes import message_id  # noqa: F401
from mimir.web.routes import search  # noqa: F401
from mimir.web.routes import sitemaps  # noqa: F401
from mimir.web.routes import static_meta  # noqa: F401
from mimir.web.routes import timeviews  # noqa: F401
