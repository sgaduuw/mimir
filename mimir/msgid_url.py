"""Which Message-ID a message URL names.

The tree walker's `Link:` trailers and the body linkifier's `(local)`
links ask the same question, so they share this one answer and a host
learned by one is learned by both (#668).
"""

from urllib.parse import unquote, urlsplit

# Host -> the path prefix under which it names a Message-ID; "/" is any path.
# `patch.msgid.link/<msgid>` is b4's default `Link:` since 2024; `msgid.link`
# is its alias. The other hosts name one only under a prefix, and their other
# paths (`lkml.kernel.org/lkml/...`, `patchwork.freedesktop.org/patch/<n>`) do
# not. `lkml.kernel.org/r/<msgid>` redirects a Message-ID to lore (tip tree
# and Andrew Morton's scripts, 2011 on); `/n/tip-...@git.kernel.org` is a
# tip-tree synthetic tag that matches no posted message, so it is excluded on
# purpose. `patchwork.freedesktop.org/patch/msgid/<msgid>` is the i915/xe/drm
# form.
MSGID_URL_PREFIXES = {
    "lore.kernel.org": "/",
    "patch.msgid.link": "/",
    "msgid.link": "/",
    "lkml.kernel.org": "/r/",
    "patchwork.freedesktop.org": "/patch/msgid/",
}


def msgid_from_url(url: str) -> str | None:
    """Return the Message-ID a known msgid URL names, or `None`.

    Recognises every shape seen in the wild:

        https://lore.kernel.org/<msgid>
        https://lore.kernel.org/<slug>/<msgid>(/T/|/raw|/t.mbox.gz|/...)?
        https://patch.msgid.link/<msgid>
        http://lkml.kernel.org/r/<msgid>
        https://patchwork.freedesktop.org/patch/msgid/<msgid>
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
    prefix = MSGID_URL_PREFIXES.get(parts.netloc.lower())
    if prefix is None or not parts.path.startswith(prefix):
        return None
    for segment in parts.path.split("/"):
        if "@" in segment:
            return unquote(segment)
    return None
