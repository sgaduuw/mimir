"""SEO output, split across three submodules:

- `sitemaps`: `/sitemap.xml`, `/meta-sitemap.xml`, `/<inbox>/sitemap.xml`
  (XML rendering + per-inbox-latest helper).
- `json_ld`: schema.org payload builders, one per page shape (index,
  inbox, message, search, author).
- `atom`: Atom 1.0 feed renderer used by per-inbox and per-author feeds.

A few JSON-LD / Atom helpers reach back into `mimir.web` for the
shared display filters (`_msg_url`, `_display_name_filter`,
`_redact_trailer_address`). Those imports are done inside the
function bodies to avoid an import-time cycle (web imports
sitemap/JSON-LD builders from this package at module load).

Cache keys are stable: bumping `cache.NAMESPACE_VERSION` invalidates
everything if a payload shape changes, so per-route expiry is purely
about freshness, not correctness.
"""

from mimir.seo.atom import atom_response
from mimir.seo.json_ld import (
    DEFAULT_SITE_DESCRIPTION,
    JSON_LD_TEXT_MAX,
    _json_ld_author,
    _json_ld_index,
    _json_ld_inbox,
    _json_ld_message,
    _json_ld_search,
    _json_ld_text_snippet,
)
from mimir.seo.sitemaps import (
    SITEMAP_RECENT_PER_INBOX,
    SITEMAP_TTL_SEC,
    inbox_sitemap_xml,
    meta_sitemap_xml,
    sitemap_index_xml,
)

__all__ = [
    "DEFAULT_SITE_DESCRIPTION",
    "JSON_LD_TEXT_MAX",
    "SITEMAP_RECENT_PER_INBOX",
    "SITEMAP_TTL_SEC",
    "_json_ld_author",
    "_json_ld_index",
    "_json_ld_inbox",
    "_json_ld_message",
    "_json_ld_search",
    "_json_ld_text_snippet",
    "atom_response",
    "inbox_sitemap_xml",
    "meta_sitemap_xml",
    "sitemap_index_xml",
]
