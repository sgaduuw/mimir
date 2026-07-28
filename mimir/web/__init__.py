"""Web blueprint package.

`bp_web` lives in `_blueprint` to keep route/filter/hook submodules
free of import cycles. This `__init__.py` triggers the side-effect
imports that register routes, template filters, hooks, and the
context processor; it also re-exports the names that callers
(`mimir.cli`, `mimir.seo`, `mimir.indexnow`, and the test suite)
have always imported from `mimir.web`, so the split is a pure
refactor at the import boundary.
"""

from mimir.web._blueprint import bp_web

# Import order matters: `filters` and `hooks` attach decorators to
# `bp_web`; `routes` triggers each route submodule's own
# registration. Pull them all in before any caller can render a
# response.
from mimir.web import filters  # noqa: F401  (template filters)
from mimir.web import hooks  # noqa: F401  (context processor + request hooks)
from mimir.web import routes  # noqa: F401  (route handlers via the routes subpackage)
from mimir.web import errors  # noqa: F401  (branded 4xx/5xx handlers)

# Re-exports for backward-compatible imports from `mimir.web`.
# Routes / helpers callers have historically imported from this
# module by direct name; the package layout keeps those names
# reachable from the same path.
from mimir.seo import (
    inbox_sitemap_xml,
    maintainers_sitemap_xml,
    meta_sitemap_xml,
    sitemap_index_xml,
)
from mimir.web.filters import (
    _allowlisted_email,
    _clean_subject_filter,
    _display_name_filter,
    _is_allowlisted,
    _is_allowlisted_address_filter,
    _is_previewable,
    _is_previewable_filter,
    _lexer_for,
    _msg_url_filter,
    _patch_synthesis_filter,
    _redact_trailer_address,
    _relative_time,
    _relative_time_filter,
    _render_body_filter,
    _safe_from_filter,
    _thread_summary,
)
from mimir.web.routes.dashboards import (
    RECENT_PAGE_SIZE,
    SUBSYSTEM_RECENT_PATCHES_LIMIT,
    _fetch_recent,
    index,
    inbox_dashboard,
    subsystem_dashboard,
)
from mimir.web.routes.feeds import (
    FEED_ENTRY_LIMIT,
    author_feed,
    inbox_feed,
)
from mimir.web.routes.message import message
from mimir.web.routes.search import (
    AUTHOR_VIEW_LIMIT,
    SEARCH_QUERY_MAX_LEN,
    SEARCH_QUERY_MIN_LEN,
    SEARCH_RESULT_CAP,
    author_view,
    reviewer_view,
    search,
)
from mimir.web.routes.static_meta import (
    OG_IMAGE_ALT,
    OG_IMAGE_FILENAME,
    OG_IMAGE_HEIGHT,
    OG_IMAGE_WIDTH,
)
from mimir.web.routes.timeviews import MONTH_THREAD_CAP
from mimir.web.routes.attachments import _content_disposition
from mimir.web.urls import (
    _canonical_inbox_name,
    _advertised_urls_for,
    _canonical_inbox_names_for,
    _canonical_url_for,
    _get_inbox_or_404,
    _msg_url,
    _site_base,
    _thread_view_url,
    _year_decade_groups,
)

__all__ = [
    "AUTHOR_VIEW_LIMIT",
    "FEED_ENTRY_LIMIT",
    "MONTH_THREAD_CAP",
    "OG_IMAGE_ALT",
    "OG_IMAGE_FILENAME",
    "OG_IMAGE_HEIGHT",
    "OG_IMAGE_WIDTH",
    "RECENT_PAGE_SIZE",
    "SEARCH_QUERY_MAX_LEN",
    "SEARCH_QUERY_MIN_LEN",
    "SEARCH_RESULT_CAP",
    "SUBSYSTEM_RECENT_PATCHES_LIMIT",
    "_allowlisted_email",
    "_canonical_inbox_name",
    "_advertised_urls_for",
    "_canonical_inbox_names_for",
    "_canonical_url_for",
    "_clean_subject_filter",
    "_content_disposition",
    "_display_name_filter",
    "_fetch_recent",
    "_get_inbox_or_404",
    "_is_allowlisted",
    "_is_allowlisted_address_filter",
    "_is_previewable",
    "_is_previewable_filter",
    "_lexer_for",
    "_msg_url",
    "_msg_url_filter",
    "_patch_synthesis_filter",
    "_redact_trailer_address",
    "_relative_time",
    "_relative_time_filter",
    "_render_body_filter",
    "_safe_from_filter",
    "_site_base",
    "_thread_view_url",
    "_thread_summary",
    "_year_decade_groups",
    "author_feed",
    "author_view",
    "bp_web",
    "inbox_dashboard",
    "inbox_feed",
    "inbox_sitemap_xml",
    "index",
    "maintainers_sitemap_xml",
    "message",
    "meta_sitemap_xml",
    "reviewer_view",
    "search",
    "sitemap_index_xml",
    "subsystem_dashboard",
]
