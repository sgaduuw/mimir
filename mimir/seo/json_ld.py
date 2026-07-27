"""schema.org JSON-LD payloads, one builder per page shape.

`_json_ld_index` (`/`), `_json_ld_inbox` (`/<inbox>/`),
`_json_ld_message` (message page, the only one with a `@graph`:
`DiscussionForumPosting` + `BreadcrumbList`), `_json_ld_search`
(rendered `?q=…` results page), and `_json_ld_author`
(`/<inbox>/author/<sub>`).

A few helpers reach back into `mimir.web` for the shared display
filters (`_msg_url`, `_display_name_filter`, `_redact_trailer_address`).
Those imports are done inside the function bodies to avoid an
import-time cycle (web imports JSON-LD builders from this module at
module load).
"""

from collections.abc import Sequence
from datetime import timezone
from urllib.parse import quote

from mimir.config import settings
from mimir.models import Article
from mimir.parser import ParsedArticle
from mimir.rendering import redact_trailer_addresses

JSON_LD_TEXT_MAX = 2000

# Site-wide tagline. Mirrored verbatim by base.html's default
# meta_description block so the WebSite JSON-LD and the meta tag
# can't drift. If you change one, change the other.
DEFAULT_SITE_DESCRIPTION = (
    "Linux kernel mailing list archives. ~200 inboxes indexed with "
    "cross-list deduplication, subsystem dashboards, patch-series "
    "timelines, and reviewer activity surfaces."
)


def _json_ld_index(base: str, inboxes=()) -> dict:
    """schema.org WebSite for the meta-index `/`, paired with an
    `ItemList` of configured inboxes so search engines can treat the
    page as a topical hub rather than a flat link list. Sitelinks-
    search-box is intentionally omitted (mimir's search is per-inbox,
    not site-wide)."""
    payload: dict = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": settings.site_name,
        "url": base + "/",
        "description": DEFAULT_SITE_DESCRIPTION,
    }
    if inboxes:
        payload["mainEntity"] = {
            "@type": "ItemList",
            "name": f"Inboxes indexed by {settings.site_name}",
            "numberOfItems": len(inboxes),
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": i + 1,
                    "url": f"{base}/{inbox.name}/",
                    "name": inbox.name,
                }
                for i, inbox in enumerate(inboxes)
            ],
        }
    return payload


def _json_ld_inbox(base: str, inbox, active_threads=()) -> dict:
    """schema.org payload for `/<inbox_name>/`, a `DiscussionForum`
    container plus an `ItemList` of the currently-most-active threads
    so the page reads as a topical hub for crawlers. `active_threads`
    is whatever the dashboard fetched (root-level ThreadNode objects);
    we project just the bits search engines care about (URL + name).
    """
    # Lazy imports break a `web → seo → web` cycle: these helpers
    # live in web.py with the rest of the display filters.
    from mimir.web import _clean_subject_filter, _msg_url

    payload: dict = {
        "@context": "https://schema.org",
        "@type": "DiscussionForum",
        "name": inbox.name,
        "url": f"{base}/{inbox.name}/",
    }
    if active_threads:
        payload["mainEntity"] = {
            "@type": "ItemList",
            "name": f"Most active threads in {inbox.name}",
            "numberOfItems": len(active_threads),
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": i + 1,
                    "url": f"{base}{_msg_url(t, inbox.name)}",
                    "name": _clean_subject_filter(t.subject) or "(no subject)",
                }
                for i, t in enumerate(active_threads)
            ],
        }
    return payload


def _json_ld_text_snippet(body: str | None) -> str | None:
    """Return a plaintext snippet of `body` suitable for the
    DiscussionForumPosting `text` field, or None when there's nothing
    usable. Whitespace is collapsed (mail bodies have lots of hard
    wraps that read as paragraph noise in JSON-LD); truncation
    happens at the last whitespace inside JSON_LD_TEXT_MAX so we
    don't slice mid-word, with a trailing ellipsis when we did
    truncate. Returning None lets the caller omit the field
    entirely, emitting an empty string would re-fail Google's
    "either text/image/video" validator."""
    if not body:
        return None
    collapsed = " ".join(body.split())
    if not collapsed:
        return None
    if len(collapsed) <= JSON_LD_TEXT_MAX:
        return collapsed
    head = collapsed[:JSON_LD_TEXT_MAX]
    cut = head.rfind(" ")
    # Pathological no-space body: hard-cut at the limit rather than
    # returning the entire string just because rfind didn't find a
    # break point.
    if cut <= 0:
        cut = JSON_LD_TEXT_MAX
    return head[:cut].rstrip() + "..."


def _json_ld_message(
    article: Article,
    parsed: ParsedArticle,
    canonical_url: str,
    inbox_name: str,
    base: str,
    *,
    reply_count: int,
    subsystem_names: Sequence[str],
) -> dict:
    """schema.org @graph carrying both DiscussionForumPosting (the
    primary signal, eligible for Google's "Discussions and forums"
    rich-result section) and BreadcrumbList (surfaces the
    Site → Inbox → Subject chain in SERPs).

    Author goes through `_display_name_filter` for `Person.name`,
    display name only and no `<hidden>` placeholder. The placeholder
    is a rendering decision for the visible HTML; in machine-readable
    metadata it reads as broken data and was flagged as such in the
    2026-05-12 review. `Person.email` is conditionally added via
    `_allowlisted_email`: present iff the sender is in the allowlist
    union (and therefore already on the rendered page in full),
    omitted otherwise. Matches the redaction posture of
    `_safe_from_filter` on the HTML side. `author.url` points at the
    per-inbox author view so the Person has a stable target for
    "more posts by this author"; required-by-Google for the
    Discussions rich-result eligibility (non-critical, Search
    Console 2026-05-14). `dateModified` mirrors `datePublished`
    because mimir doesn't track edits.

    `text` carries a plain-text snippet of `parsed.body`, capped at
    JSON_LD_TEXT_MAX chars (truncated at the last whitespace inside
    the cap), Google's DiscussionForumPosting validator treats one
    of `text` / `image` / `video` as required (critical, Search
    Console 2026-05-14). Omitted entirely when the body is missing
    or whitespace-only: an empty string would re-fail the validator.

    Prefers `parsed.date` (the original RFC 5322 Date header) over
    `article.date` (the public-inbox commit time), the message's
    actual send date is more meaningful to search engines.

    `reply_count` / `subsystem_names` carry the discussion + topical
    signals mimir already computes for the rendered page, so the
    structured data reflects the same uniquely-mimir data a reader
    sees (SEO index-shaping design, 2026-07-27 W3a):

    - `interactionStatistic` counts replies to **this** message
      (direct children in the thread graph), not the whole thread.
      A DiscussionForumPosting is the single message, so claiming the
      thread's total here would be inaccurate structured data on every
      reply page. Omitted entirely at zero, an explicit
      `userInteractionCount: 0` is noise, not signal.
    - `about` / `keywords` carry the matched subsystem names. These
      are the genuinely topical terms for the page (`net`, `bcachefs`).
      The `[PATCH v3]`-style subject tag is deliberately NOT emitted as
      a keyword: nobody searches "PATCH v3", so it would dilute the
      keyword set with boilerplate rather than describe the subject
      matter."""
    # Lazy imports break the `web → seo → web` cycle (see module
    # docstring). The redaction helpers and display filter live in
    # web.py with the rest of the visible-HTML pipeline.
    from mimir.web import (
        _allowlisted_email,
        _display_name_filter,
        _redact_trailer_address,
    )

    raw_date = parsed.date or article.date
    if raw_date is not None and raw_date.tzinfo is None:
        # `-0000` Date headers come back tz-naive from
        # parsedate_to_datetime; emit aware UTC so consumers don't
        # see schema-invalid bare datetimes.
        raw_date = raw_date.replace(tzinfo=timezone.utc)
    iso_date = raw_date.strftime("%Y-%m-%dT%H:%M:%S%z") if raw_date else None
    subject = parsed.subject or "(no subject)"
    breadcrumb_subject = subject if len(subject) <= 80 else subject[:77] + "..."
    author_name = _display_name_filter(parsed.author)
    author: dict = {"@type": "Person", "name": author_name}
    author_email = _allowlisted_email(parsed.author)
    if author_email:
        author["email"] = author_email
    # Per-inbox author view is a substring match on the From field;
    # the display name is exactly what'll match the author's other
    # posts. Skip the URL when we fell back to "unknown sender"
    # that token doesn't match anyone.
    if author_name and author_name != "unknown sender":
        author["url"] = f"{base}/{inbox_name}/author/{quote(author_name, safe='')}"
    forum_post: dict = {
        "@type": "DiscussionForumPosting",
        "@id": canonical_url,
        "url": canonical_url,
        "mainEntityOfPage": canonical_url,
        "headline": subject,
        "author": author,
        "isPartOf": {
            "@type": "WebSite",
            "name": inbox_name,
            "url": f"{base}/{inbox_name}/",
        },
    }
    # Apply the same DCO trailer redaction the visible HTML uses
    # before snippeting, JSON-LD `text` is yet another surface a
    # crawler scrapes, and CONTEXT.md's redaction invariants treat
    # every surface uniformly. Without this, non-allowlisted
    # Signed-off-by addresses would leak through the structured
    # data even though the rendered page redacts them.
    redacted_body = (
        redact_trailer_addresses(parsed.body, _redact_trailer_address)
        if parsed.body
        else parsed.body
    )
    body_snippet = _json_ld_text_snippet(redacted_body)
    if body_snippet:
        forum_post["text"] = body_snippet
    if iso_date:
        forum_post["datePublished"] = iso_date
        forum_post["dateModified"] = iso_date
    if reply_count > 0:
        forum_post["interactionStatistic"] = {
            "@type": "InteractionCounter",
            "interactionType": "https://schema.org/ReplyAction",
            "userInteractionCount": reply_count,
        }
    if subsystem_names:
        forum_post["about"] = [
            {"@type": "Thing", "name": name} for name in subsystem_names
        ]
        forum_post["keywords"] = list(subsystem_names)
    return {
        "@context": "https://schema.org",
        "@graph": [
            forum_post,
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": 1,
                        "name": settings.site_name,
                        "item": base + "/",
                    },
                    {
                        "@type": "ListItem",
                        "position": 2,
                        "name": inbox_name,
                        "item": f"{base}/{inbox_name}/",
                    },
                    {
                        "@type": "ListItem",
                        "position": 3,
                        "name": breadcrumb_subject,
                        "item": canonical_url,
                    },
                ],
            },
        ],
    }


def _json_ld_search(
    base: str,
    inbox_name: str,
    query: str,
    canonical_url: str,
) -> dict:
    """schema.org `SearchResultsPage` for `/<inbox_name>/search?q=…`.
    Emitted only when the route is rendering actual results (the
    no-query / too-short forms are just a search box, not a results
    page). `url` mirrors the canonical, which strips the query
    string, same SEO posture as the `<link rel="canonical">`: this
    is the page-shape, not the result-set."""
    return {
        "@context": "https://schema.org",
        "@type": "SearchResultsPage",
        "name": f"Search results for '{query}' in {inbox_name}",
        "url": canonical_url,
        "description": f"Search results for '{query}' in {inbox_name}.",
        "isPartOf": {
            "@type": "WebSite",
            "name": inbox_name,
            "url": f"{base}/{inbox_name}/",
        },
    }


def _json_ld_author(
    base: str,
    inbox_name: str,
    sub: str,
    canonical_url: str,
) -> dict:
    """schema.org `ProfilePage` for `/<inbox_name>/author/<sub>`. The
    `mainEntity` is a `Person` whose `name` is the sender substring
    we matched against, usually a full email or a domain like
    `@kernel.org`, sometimes a personal display-name fragment. We
    don't try to resolve it to a single identity (the substring may
    match many people, deliberately so for `@kernel.org`-shaped
    queries); `name` is the literal token the page is indexed against.
    """
    return {
        "@context": "https://schema.org",
        "@type": "ProfilePage",
        "name": f"Messages from {sub} in {inbox_name}",
        "url": canonical_url,
        "description": (
            f"Recent messages from senders matching '{sub}' "
            f"in the {inbox_name} archive."
        ),
        "mainEntity": {
            "@type": "Person",
            "name": sub,
        },
        "isPartOf": {
            "@type": "WebSite",
            "name": inbox_name,
            "url": f"{base}/{inbox_name}/",
        },
    }
