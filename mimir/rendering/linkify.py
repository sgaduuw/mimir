"""URL + Message-ID linkification and DCO trailer redaction.

`linkify` is the per-text-block entry called by the orchestrator in
`body.py` for `kind == "text"` blocks. It walks lines, routes DCO
trailer lines (`Signed-off-by:`, `Reviewed-by:`, …) through the
trailer-aware variant `_render_trailer_line` (which redacts
`<email>` patterns via the caller-supplied redactor), and routes
everything else through `_render_line` (which linkifies URLs and
`<Message-IDs>`).

Bodies often reference other messages via lore.kernel.org URLs
(`https://lore.kernel.org/<slug>/<msgid>/...`). When the embedded
Message-ID resolves to something mimir indexed, the renderer
appends ` (<a>local</a>)` after the lore URL so readers can stay
on-site, the lore URL itself is preserved (the caller asked to
"add, not replace"). The msgid→mirror-URL map is built by the
route and threaded through `linkify`/`render_body` as
`lore_mirror_urls`.

`redact_trailer_addresses` is the plain-text counterpart used by
JSON-LD code that needs the redaction posture applied to body
bytes without going through the HTML pipeline.
"""
import html
import re
from urllib.parse import unquote, urlsplit

URL_OR_MSGID_RE = re.compile(
    r'(?P<url>https?://[^\s<>"\'\]\)]+)'
    r"|<(?P<msgid>[A-Za-z0-9._%+\-=$/]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>"
)
# DCO trailers are process attestations, not contact info. We still
# redact addresses that aren't allowlisted, but we don't want the
# msgid linkifier to mistake the address for a missing message-ID
# and emit `[off-list ref]`, that actively looks like broken
# metadata and breaks DCO chain verification on display.
TRAILER_KEYS = (
    "Signed-off-by",
    "Reviewed-by",
    "Tested-by",
    "Acked-by",
    "Co-developed-by",
    "Reported-by",
    "Suggested-by",
    "Reported-and-tested-by",
    "Cc",
    "To",
    "From",
)
_TRAILER_LINE_RE = re.compile(
    r"^(?:" + "|".join(re.escape(k) for k in TRAILER_KEYS) + r"):\s",
    re.IGNORECASE,
)
# Local-part / domain restricted to a conservative RFC 5322 subset that
# excludes HTML metacharacters (`"`, `'`, `<`, `=`, etc.). The trailer
# renderer also html-escapes the redactor's return value as defense in
# depth; tightening here means hostile addresses fall through to the
# default `_render_line` path (which always escapes) instead of going
# through the redactor branch at all.
_EMAIL_ANGLE_RE = re.compile(r"<([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+)>")


def _extract_lore_msgid(url: str) -> str | None:
    """Return the Message-ID embedded in a `lore.kernel.org` URL, or
    `None` if `url` isn't a lore link or has no msgid-shaped path
    segment.

    Recognises every lore URL shape we've seen in the wild:

        https://lore.kernel.org/<msgid>
        https://lore.kernel.org/<slug>/<msgid>(/T/|/raw|/t.mbox.gz|/...)?
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
    if parts.netloc.lower() != "lore.kernel.org":
        return None
    for segment in parts.path.split("/"):
        if "@" in segment:
            return unquote(segment)
    return None


def linkify(
    text: str,
    msgid_urls: dict[str, str] | None = None,
    address_redactor=None,
    lore_mirror_urls: dict[str, str] | None = None,
) -> str:
    """Escape `text` and replace URLs and `<Message-IDs>` with anchor tags.

    Message-IDs are *not* rendered verbatim, they're email-shaped (often
    with @host, sometimes with the literal local-part), and rendering
    them in body text would re-leak the address info we already
    de-leaked from URLs. Instead they collapse to a neutral placeholder:
    `[ref]` (with link, when in archive) or `[off-list ref]` (when not).
    The link still points at the canonical archive URL.

    `address_redactor` is an optional callable ``(email_str) -> str``
    applied to ``<email>`` patterns on DCO trailer lines
    (``Signed-off-by:``, ``Reviewed-by:``, etc.). The callable returns
    *plain text*, typically ``<addr@dom>`` for allowlisted senders
    and ``<redacted>`` otherwise; the renderer html-escapes the
    return value before splicing into output, so the redactor cannot
    smuggle live HTML regardless of what it returns. Without a
    redactor, trailer lines fall through to the msgid linkifier and
    emit ``[off-list ref]`` which actively looks like broken DCO
    metadata.

    `lore_mirror_urls` is an optional ``{msgid: mimir_url}`` dict.
    When a URL match is a `lore.kernel.org` link whose embedded
    Message-ID is in the dict, ``(<a>local</a>)`` is appended after
    the lore anchor so the reader can stay on-site. The lore URL
    itself is not modified.
    """
    msgid_urls = msgid_urls or {}
    lore_mirror_urls = lore_mirror_urls or {}
    rendered_lines: list[str] = []
    for line in text.split("\n"):
        if address_redactor is not None and _TRAILER_LINE_RE.match(line):
            rendered_lines.append(_render_trailer_line(
                line, address_redactor, lore_mirror_urls,
            ))
        else:
            rendered_lines.append(_render_line(line, msgid_urls, lore_mirror_urls))
    return "\n".join(rendered_lines)


def _local_mirror_suffix(url: str, lore_mirror_urls: dict[str, str]) -> str:
    """Return ` (<a href="...">local</a>)` (leading space included)
    if `url` is a lore URL with an indexed msgid, else ``""``.

    Centralises the "extract + lookup + render" trio so the regular
    text path and the trailer path stay in sync."""
    if not lore_mirror_urls:
        return ""
    mid = _extract_lore_msgid(url)
    if mid is None:
        return ""
    mirror = lore_mirror_urls.get(mid)
    if not mirror:
        return ""
    return f' (<a href="{html.escape(mirror)}">local</a>)'


def _render_line(
    line: str,
    msgid_urls: dict[str, str],
    lore_mirror_urls: dict[str, str],
) -> str:
    out: list[str] = []
    pos = 0
    for m in URL_OR_MSGID_RE.finditer(line):
        out.append(html.escape(line[pos : m.start()]))
        if m.group("url"):
            url = m.group("url").rstrip(".,;:!?")
            esc = html.escape(url)
            out.append(f'<a href="{esc}" rel="nofollow">{esc}</a>')
            out.append(_local_mirror_suffix(url, lore_mirror_urls))
            pos = m.start() + len(m.group("url"))
            trimmed = m.group("url")[len(url):]
            if trimmed:
                out.append(html.escape(trimmed))
        else:
            mid = m.group("msgid")
            href = msgid_urls.get(mid)
            if href:
                out.append(f'<a href="{html.escape(href)}">[ref]</a>')
            else:
                out.append("[off-list ref]")
            pos = m.end()
    out.append(html.escape(line[pos:]))
    return "".join(out)


def _render_trailer_line(
    line: str, redactor, lore_mirror_urls: dict[str, str],
) -> str:
    """Trailer-line variant of `_render_line`: redacts ``<email>``
    patterns via `redactor` instead of msgid-linkifying them. URLs
    elsewhere on the line still get the normal URL treatment,
    including the lore-URL "(local)" mirror suffix.

    The redactor returns *plain text*, including the literal angle
    brackets it wants visible. This renderer html-escapes the return
    value before splicing into output, so a redactor cannot smuggle
    live HTML through this code path regardless of what the email
    regex matched.
    """
    out: list[str] = []
    pos = 0
    spans: list[tuple[int, int, str]] = []
    for m in URL_OR_MSGID_RE.finditer(line):
        if m.group("url"):
            url = m.group("url").rstrip(".,;:!?")
            esc = html.escape(url)
            replacement = f'<a href="{esc}" rel="nofollow">{esc}</a>'
            replacement += _local_mirror_suffix(url, lore_mirror_urls)
            trimmed = m.group("url")[len(url):]
            if trimmed:
                replacement += html.escape(trimmed)
            spans.append((m.start(), m.end(), replacement))
    for m in _EMAIL_ANGLE_RE.finditer(line):
        spans.append((m.start(), m.end(), html.escape(redactor(m.group(1)))))
    spans.sort()
    for start, end, replacement in spans:
        out.append(html.escape(line[pos:start]))
        out.append(replacement)
        pos = end
    out.append(html.escape(line[pos:]))
    return "".join(out)


def redact_trailer_addresses(text: str, redactor) -> str:
    """Plain-text variant of the DCO trailer redaction the HTML
    renderer applies via `_render_trailer_line`. Walks the body
    line-by-line, applies `redactor` to every `<email>` pattern on
    a `Signed-off-by:` / `Reviewed-by:` / etc. line, and returns
    plaintext with the substitutions in place. Used by surfaces
    that consume body content outside the HTML pipeline (JSON-LD
    `text` snippet today) so the redaction posture stays
    consistent across every place a body byte can flow."""
    out: list[str] = []
    for line in text.split("\n"):
        if _TRAILER_LINE_RE.match(line):
            out.append(_EMAIL_ANGLE_RE.sub(lambda m: redactor(m.group(1)), line))
        else:
            out.append(line)
    return "\n".join(out)
