"""Atom 1.0 feed renderer.

Uses stdlib ElementTree, no extra dep. Emits redacted authors via the
same display-name filter the HTML side uses so private email addresses
don't leak via feed readers either. The `<id>` tag URI uses the
canonical inbox name so cross-posted entries collapse to a single id
across feeds (readers that key on `<id>` won't show duplicates).

Lazy-imports `mimir.web` display helpers inside the function body to
avoid an import-time cycle.
"""
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring

from flask import Response

from mimir.dashboard import ArticleSummary

# Tag-URI host portion (RFC 4151). Constant rather than the request
# host so feed entry IDs stay stable across re-deployments and aren't
# tied to whatever proxy the request came in through.
_TAG_URI_AUTHORITY = "mimir"


def atom_response(
    *,
    feed_id: str,
    feed_title: str,
    self_url: str,
    alternate_url: str,
    entries: list[ArticleSummary],
    inbox_name: str,
    base_url: str,
    canonical_inbox_by_article: dict[int, str] | None = None,
) -> Response:
    """Render an Atom 1.0 feed from a list of `ArticleSummary`. Uses
    stdlib ElementTree, no extra dep. Emits redacted authors via the
    same `safe_from` rule the HTML side uses, so private email
    addresses don't leak via feed readers either.

    `canonical_inbox_by_article` maps article.id → canonical inbox name.
    Cross-posts get their `<id>` and `<link>` set to the canonical URL
    so feed readers that key on `<id>` deduplicate across feeds (the
    same article appearing in lkml's and linux-fsdevel's feeds renders
    as one entry, not two)."""
    # Lazy imports break the `web → seo → web` cycle (see module
    # docstring).
    from mimir.web import _display_name_filter, _msg_url
    feed_updated = (
        max((e.date for e in entries if e.date), default=None)
        or datetime.now(timezone.utc)
    )
    canonical_map = canonical_inbox_by_article or {}

    feed = Element("feed", xmlns="http://www.w3.org/2005/Atom")
    SubElement(feed, "id").text = feed_id
    SubElement(feed, "title").text = feed_title
    SubElement(feed, "updated").text = feed_updated.strftime("%Y-%m-%dT%H:%M:%SZ")
    SubElement(feed, "link", rel="self", type="application/atom+xml", href=self_url)
    SubElement(feed, "link", rel="alternate", type="text/html", href=alternate_url)
    SubElement(feed, "generator").text = "mimir"

    msg_base = base_url.rstrip("/")
    for a in entries:
        entry = SubElement(feed, "entry")
        # RFC 4151 tag URI. Use the canonical inbox name in the tag so
        # cross-posted entries collapse to a single id across feeds
        # readers that key on <id> won't show duplicates.
        date_str = a.date.strftime("%Y-%m-%d") if a.date else "1970-01-01"
        canonical_inbox_name = canonical_map.get(a.id, inbox_name)
        SubElement(entry, "id").text = (
            f"tag:{_TAG_URI_AUTHORITY},{date_str}:{canonical_inbox_name}/{a.id}"
        )
        SubElement(entry, "title").text = a.subject or "(no subject)"
        if a.date is not None:
            SubElement(entry, "updated").text = a.date.strftime("%Y-%m-%dT%H:%M:%SZ")
        SubElement(
            entry, "link",
            rel="alternate", type="text/html",
            href=msg_base + _msg_url(a, canonical_inbox_name),
        )
        if a.author:
            author_el = SubElement(entry, "author")
            # Display name only, same posture as JSON-LD's author.name.
            # Feed readers render <author><name> as the byline; the
            # `<hidden>` placeholder reads as broken metadata there
            # exactly as it did in JSON-LD before the 2026-05-12 fix.
            SubElement(author_el, "name").text = _display_name_filter(a.author)

    body = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        + tostring(feed, encoding="unicode")
    )
    return Response(body, mimetype="application/atom+xml; charset=utf-8")
