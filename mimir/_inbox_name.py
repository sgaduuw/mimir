"""The one rule for what an inbox name may be.

A name flows into URL paths and cache-key fragments, so it must be
URL-safe and `:`-free: lowercase, alphanumeric and hyphens, not starting
or ending with a hyphen, at most 64 characters.

Its own module because `mimir.config` checks `INBOXES` keys against it
at load (#747) and cannot import `mimir.inboxes`, which imports
`config` through `cache`.
"""

import re

INBOX_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def inbox_path(name: str) -> str:
    """Site-relative URL of an inbox's dashboard. The page's canonical,
    its sitemap entry and the `/llms.txt` link all spell it from here, so
    they are one string: a link that differs from the canonical by a
    slash is a duplicate-URL signal."""
    return f"/{name}/"
