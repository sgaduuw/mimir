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
from urllib.parse import quote

from mimir.config import settings
from mimir.datetime_utils import aware_utc
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


def _thread_url_for(thread, inbox_name: str) -> str:
    """Whole-thread URL for an `ActiveThread` with replies, else its
    message URL.

    `reply_count > 0` is exactly "this thread has more than one message
    in this inbox", which is the consolidation rule, and it needs no
    query. The equivalence is worth spelling out because the field is a
    WINDOWED count and that looks like it should break it:

    - `reply_count > 0` means at least one non-root message was seen in
      the window, so the thread has at least two messages. Sound.
    - `reply_count == 0` does NOT prove the thread is a single message,
      and an earlier version of this docstring claimed it did. The
      argument was "replies can only follow their root in time, so a
      root inside the window cannot have replies outside it". That
      relies on `articles.date` being send time. It is not: it is the
      public-inbox commit time, i.e. ARCHIVAL order (see CONTEXT.md
      "articles.date = public-inbox commit timestamp"). A child
      archived before its parent is a documented, handled reality here
      (`ingest/_pending.py`, "a child ingested before its parent, which
      happens across epoch boundaries"), and in that case the reply's
      date is earlier than its root's, so it can fall outside a window
      the root is inside.

      The consequence is bounded and in the safe direction: such a
      thread is UNDER-consolidated, advertised as a message URL exactly
      as it was before this change, so it is an incomplete fix rather
      than a regression. Closing it properly needs a real per-thread
      count, i.e. a query on a hot page, which is not worth it for a
      hint; if this list ever becomes load-bearing, that is the trade
      to revisit.

    Inbox-scoped on purpose, matching what it replaces: this list is
    rendered on `/<inbox>/`, so its URLs stay in that inbox rather than
    hopping to each thread's canonical inbox. `reply_count` is computed
    per inbox too, so the count and the URL agree.

    Also immune to the cyclic-`thread_parent` inflation that
    `dedupe_thread` exists to absorb: this counts recent messages
    grouped by root, not a downward walk, so a cycle cannot re-emit an
    article and fake a multi-message thread.
    """
    from mimir.web import _msg_url, _thread_view_url

    if thread.reply_count > 0:
        return _thread_view_url(thread, inbox_name)
    return _msg_url(thread, inbox_name)


def _json_ld_inbox(base: str, inbox, active_threads=()) -> dict:
    """schema.org payload for `/<inbox_name>/`, a `DiscussionForum`
    container plus an `ItemList` of the currently-most-active threads
    so the page reads as a topical hub for crawlers. `active_threads`
    is whatever the dashboard fetched (root-level ThreadNode objects);
    we project just the bits search engines care about (URL + name).
    """
    # Lazy imports break a `web → seo → web` cycle: these helpers
    # live in web.py with the rest of the display filters.
    from mimir.web import _clean_subject_filter

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
                    "url": f"{base}{_thread_url_for(t, inbox.name)}",
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


BREADCRUMB_NAME_MAX = 80


UNKNOWN_SENDER = "unknown sender"


def _person(raw: str | None, *, base: str, inbox_name: str, with_url: bool) -> dict:
    """schema.org `Person` for a message sender, redaction-aware.

    `name` is the display name only (never the `<hidden>` placeholder,
    which reads as broken data in metadata even though it is right on
    the rendered page). `email` rides along only for allowlisted
    senders, mirroring exactly what `_safe_from_filter` shows.

    `with_url` points at the per-inbox author view, which Google wants
    as a stable "more posts by this author" target for Discussions
    eligibility. Off for thread comments: repeating the same per-inbox
    URL for every participant adds no signal.
    """
    from mimir.web import _allowlisted_email, _display_name_filter

    name = _display_name_filter(raw)
    person: dict = {"@type": "Person", "name": name}
    email = _allowlisted_email(raw)
    if email:
        person["email"] = email
    if with_url and name and name != UNKNOWN_SENDER:
        person["url"] = f"{base}/{inbox_name}/author/{quote(name, safe='')}"
    return person


def _graph_with_breadcrumbs(
    entity: dict,
    *,
    base: str,
    inbox_name: str,
    subject: str,
    canonical_url: str,
) -> dict:
    """Wrap a page entity in the `@graph` envelope plus the
    Site -> Inbox -> Subject BreadcrumbList every content page emits.

    One home for the SERP truncation rule, which was previously
    spelled once per builder; two copies of an 80/77 constant in one
    module is exactly the drift this collapses.
    """
    name = (
        subject
        if len(subject) <= BREADCRUMB_NAME_MAX
        else subject[: BREADCRUMB_NAME_MAX - 3] + "..."
    )
    return {
        "@context": "https://schema.org",
        "@graph": [
            entity,
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
                        "name": name,
                        "item": canonical_url,
                    },
                ],
            },
        ],
    }


def _iso_datetime(dt) -> str | None:
    """ISO-8601 with offset, or None. Naive datetimes (an RFC 5322
    `-0000` Date) are normalised through the project's `aware_utc`
    rather than re-implementing the tzinfo fill per builder."""
    if dt is None:
        return None
    return aware_utc(dt).strftime("%Y-%m-%dT%H:%M:%S%z")


def _json_ld_message(
    article: Article,
    parsed: ParsedArticle,
    canonical_url: str,
    inbox_name: str,
    base: str,
    *,
    reply_count: int,
    subsystem_names: Sequence[str],
    page_canonical_url: str | None = None,
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
    from mimir.web import _redact_trailer_address

    iso_date = _iso_datetime(parsed.date or article.date)
    subject = parsed.subject or "(no subject)"
    author = _person(parsed.author, base=base, inbox_name=inbox_name, with_url=True)
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
    # The breadcrumb leaf follows the PAGE's canonical, not the
    # entity's `@id`. BreadcrumbList is a SERP-rendered navigation
    # path, so pointing it at a URL the page itself disclaims (once
    # consolidation moves the canonical to the thread view) puts a
    # third URL in a document that already carries two.
    return _graph_with_breadcrumbs(
        forum_post,
        base=base,
        inbox_name=inbox_name,
        subject=subject,
        canonical_url=page_canonical_url or canonical_url,
    )


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


def _json_ld_thread(
    *,
    nodes,
    parsed_by_id: dict,
    canonical_url: str,
    inbox_name: str,
    base: str,
    total_replies: int,
    last_activity=None,
    subsystem_names: Sequence[str],
) -> dict:
    """schema.org payload for the whole-thread view: one
    `DiscussionForumPosting` for the root carrying every reply as a
    `comment` array.

    This is Google's recommended shape for a forum thread and the
    reason the thread view exists as an indexable surface: a single
    message page can only ever describe one post, while this describes
    the conversation, which is the substantial document.

    `interactionStatistic` here legitimately counts the WHOLE thread
    (unlike `_json_ld_message`, where the entity is a single message
    and the thread total would be a false claim), because the entity
    IS the thread. It reports every reply, including any past the
    render cap: the count describes the discussion, not this page's
    rendered subset.

    `nodes` are the RENDERED ThreadNodes; `parsed_by_id` maps node id
    to ParsedArticle for those whose blob resolved. A node with no
    parsed body still contributes a comment (author + date + subject
    are known from SQL), it just carries no `text`.

    Every comment body goes through the same trailer redaction as the
    visible HTML before snippeting. CONTEXT.md's redaction invariants
    apply per surface, and this surface carries N bodies rather than
    one, so a miss here would leak N times over.
    """
    from mimir.web import _clean_subject_filter, _msg_url, _redact_trailer_address

    def _text_for(node) -> str | None:
        parsed = parsed_by_id.get(node.id)
        if parsed is None or not parsed.body:
            return None
        return _json_ld_text_snippet(
            redact_trailer_addresses(parsed.body, _redact_trailer_address)
        )

    root = nodes[0]
    root_subject = _clean_subject_filter(root.subject) or "(no subject)"
    payload: dict = {
        "@type": "DiscussionForumPosting",
        "@id": canonical_url,
        "url": canonical_url,
        "mainEntityOfPage": canonical_url,
        "headline": root_subject,
        "author": _person(root.author, base=base, inbox_name=inbox_name, with_url=True),
        "isPartOf": {
            "@type": "WebSite",
            "name": inbox_name,
            "url": f"{base}/{inbox_name}/",
        },
    }
    root_text = _text_for(root)
    if root_text:
        payload["text"] = root_text
    root_date = _iso_datetime(root.date)
    if root_date:
        payload["datePublished"] = root_date
        # The thread's newest date, not `nodes[-1]`: that is the
        # depth-first-last node of the CAPPED slice, so on any branched
        # or truncated thread it understates freshness, and it can even
        # fall below datePublished when a reply carries an earlier date.
        payload["dateModified"] = _iso_datetime(last_activity) or root_date
    if total_replies > 0:
        payload["interactionStatistic"] = {
            "@type": "InteractionCounter",
            "interactionType": "https://schema.org/ReplyAction",
            "userInteractionCount": total_replies,
        }
    if subsystem_names:
        payload["about"] = [
            {"@type": "Thing", "name": name} for name in subsystem_names
        ]
        payload["keywords"] = list(subsystem_names)

    # NESTED, not flat. Every reply used to be attached directly to the
    # root, which asserts to crawlers that a reply-to-a-reply answers
    # the original post. That is a false structural claim about the one
    # document every message in the thread canonicalises to, and it is
    # the machine-readable half of the same gap a reader noticed in the
    # rendered page (2026-08-02): the hierarchy was computed, carried
    # to the template, and thrown away.
    #
    # Nesting does not grow the payload. It reparents the same comment
    # objects rather than duplicating them, so object count and total
    # text are unchanged.
    by_msgid: dict[str, dict] = {}
    for node in nodes[1:]:
        comment: dict = {
            "@type": "Comment",
            "@id": base + _msg_url(node, inbox_name),
            "url": base + _msg_url(node, inbox_name),
            "author": _person(
                node.author, base=base, inbox_name=inbox_name, with_url=False
            ),
        }
        when = _iso_datetime(node.date)
        if when:
            comment["datePublished"] = when
        body = _text_for(node)
        if body:
            comment["text"] = body
        by_msgid[node.message_id] = comment

    root_msgid = nodes[0].message_id
    comments: list[dict] = []
    for node in nodes[1:]:
        comment = by_msgid[node.message_id]
        parent = by_msgid.get(node.thread_parent or "")
        # A message whose parent is not in THIS document attaches at top
        # level: on a paginated thread the parent may be on an earlier
        # page, and inventing a container for a post the page does not
        # carry is the failure being fixed, one level down. Root-parented
        # replies land here too, which is correct.
        if parent is None or node.thread_parent == root_msgid:
            comments.append(comment)
        else:
            parent.setdefault("comment", []).append(comment)
    if comments:
        payload["comment"] = comments

    return _graph_with_breadcrumbs(
        payload,
        base=base,
        inbox_name=inbox_name,
        subject=root_subject,
        canonical_url=canonical_url,
    )
