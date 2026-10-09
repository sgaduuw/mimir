"""Which Message-ID a message URL names.

The tree walker's `Link:` trailers and the body linkifier's `(local)`
links ask the same question, so they share this one answer and a host
learned by one is learned by both (#668).
"""

from urllib.parse import unquote, urlsplit

# `patch.msgid.link/<msgid>` is b4's default `Link:` since 2024.
MSGID_URL_HOSTS = frozenset({"lore.kernel.org", "patch.msgid.link"})


def msgid_from_url(url: str) -> str | None:
    """Return the Message-ID a lore or patch.msgid.link URL names, or
    `None` if `url` is neither or has no msgid-shaped path segment.

    Recognises every shape seen in the wild:

        https://lore.kernel.org/<msgid>
        https://lore.kernel.org/<slug>/<msgid>(/T/|/raw|/t.mbox.gz|/...)?
        https://patch.msgid.link/<msgid>
        ...with or without trailing slash, query, anchor.

    The Message-ID is the first path segment containing `@`; everything
    after (e.g. `T/`, `raw`, `t.mbox.gz`) is ignored. Percent-encoded
    msgids are unquoted before return, so the result is comparable
    against the normalised value stored in `articles.message_id`.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if parts.netloc.lower() not in MSGID_URL_HOSTS:
        return None
    for segment in parts.path.split("/"):
        if "@" in segment:
            return unquote(segment)
    return None
