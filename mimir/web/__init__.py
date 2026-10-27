"""Web blueprint package.

`bp_web` lives in `_blueprint` to keep route/filter/hook submodules
free of import cycles. This `__init__.py` triggers the side-effect
imports that register routes, template filters, hooks, and the
context processor. Callers import helpers from the defining module
(`mimir.web.urls`, `mimir.web.filters`, `mimir.web.routes.<family>`);
the only re-exports are the sitemap builders, see below.
"""

# isort: off
#
# Order matters, and ruff's isort WILL rewrite it if allowed. Kept
# fenced rather than trusted, because a passing suite does not exercise
# load order: the reorder that prompted this fence left all tests green.
#
# `bp_web` first because every module below attaches to it. `filters`
# and `hooks` attach decorators; `routes` triggers each route
# submodule's own registration; `errors` last because it registers
# app-level rather than blueprint handlers. All of them before any
# caller can render a response.
#
# `mimir.seo` stays BELOW them, preserving the one-way import
# documented in `mimir/seo/__init__.py`: seo reaches back into
# `mimir.web` inside function bodies to dodge an import-time cycle, and
# that only holds while `mimir.web` is the package entered first.
from mimir.web._blueprint import bp_web
from mimir.web import filters  # noqa: F401  (template filters)
from mimir.web import hooks  # noqa: F401  (context processor + request hooks)
from mimir.web import routes  # noqa: F401  (route handlers via the routes subpackage)
from mimir.web import errors  # noqa: F401  (branded 4xx/5xx handlers)

# The sitemap builders are re-exported so `mimir.cli.cache` reaches
# them through `mimir.web`: `mimir/__init__.py` imports `mimir.cli`
# before `mimir.web`, so a direct `from mimir.seo import` there would
# enter `mimir.seo` first. Kept INSIDE the fence and below the
# side-effect imports; `test_mimir_web_is_entered_before_mimir_seo`
# holds the actual `sys.modules` entry order rather than this claim.
from mimir.seo import (
    inbox_sitemap_xml,
    maintainers_sitemap_xml,
    meta_sitemap_xml,
    sitemap_indexes_xml,
    warm_current_archive_sitemaps,
)

# isort: on

__all__ = [
    "bp_web",
    "inbox_sitemap_xml",
    "maintainers_sitemap_xml",
    "meta_sitemap_xml",
    "sitemap_indexes_xml",
    "warm_current_archive_sitemaps",
]
