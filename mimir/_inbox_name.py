"""The one rule for what an inbox name may be.

A name flows into URL paths and cache-key fragments, so it must be
URL-safe and `:`-free: lowercase, alphanumeric and hyphens, not starting
or ending with a hyphen, at most 64 characters. Literal root-route names
are reserved for the site, including lore-shaped recovery redirects.

Its own module because `mimir.config` checks `INBOXES` keys against it
at load (#747) and cannot import `mimir.inboxes`, which imports
`config` through `cache`.
"""

import re

# Every wholly literal first segment in the route map belongs to the site,
# even when punctuation would already exclude it from the slug grammar.
RESERVED_ROOT_WORDS = frozenset(
    {
        ".well-known",
        "all",
        "api",
        "apple-touch-icon-precomposed.png",
        "apple-touch-icon.png",
        "favicon.ico",
        "favicon.svg",
        "healthz",
        "llms.txt",
        "m",
        "maintainers",
        "merge-window",
        "messages-sitemap.xml",
        "meta-sitemap.xml",
        "og-image.png",
        "privacy",
        "r",
        "readyz",
        "robots.txt",
        "security.txt",
        "sitemap-maintainers.xml",
        "sitemap.xml",
        "static",
        "subsystems",
    }
)
_RESERVED_PATTERN = "|".join(re.escape(name) for name in sorted(RESERVED_ROOT_WORDS))
INBOX_NAME_RE = re.compile(
    rf"^(?!(?:{_RESERVED_PATTERN})$)[a-z0-9](?:[a-z0-9-]{{0,62}}[a-z0-9])?$"
)


def inbox_path(name: str) -> str:
    """Site-relative URL of an inbox's dashboard. The page's canonical,
    its sitemap entry and the `/llms.txt` link all spell it from here, so
    they are one string: a link that differs from the canonical by a
    slash is a duplicate-URL signal."""
    return f"/{name}/"
