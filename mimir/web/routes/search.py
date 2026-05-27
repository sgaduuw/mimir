"""Substring search, per-author view, and per-reviewer attestation
listing.

`SEARCH_QUERY_MIN_LEN` / `SEARCH_QUERY_MAX_LEN` are reused by the
author-feed clamping in `feeds.py`; that module imports them from
here.
"""

import re
from urllib.parse import quote

from flask import abort, render_template, request

from mimir.dashboard import author_recent, search_articles
from mimir.extensions import SessionLocal
from mimir.lifecycle_status import lifecycle_status_for_articles
from mimir.seo import _json_ld_author, _json_ld_search
from mimir.subsystems_dashboard import (
    REVIEWS_PER_PAGE_LIMIT,
    articles_reviewed_by,
)
from mimir.web._blueprint import bp_web
from mimir.web.urls import _get_inbox_or_404, _site_base


# Search input bounds. The query string flows into a cache key, so a
# soft length cap keeps the cache bounded and makes DoS-via-arbitrary-
# queries less interesting. Min length avoids matching the entire
# corpus on a single character.
SEARCH_QUERY_MIN_LEN = 2
SEARCH_QUERY_MAX_LEN = 80
SEARCH_RESULT_CAP = 100


AUTHOR_VIEW_LIMIT = 100


# Conservative pattern for the URL-side address: the same shape the
# trailer extractor accepts (mimir/trailers.py _TRAILER_NAME_ADDR_RE).
# Anything outside this falls to 404, defends against hostile bytes
# reaching the SQL parameter and keeps the canonical URL well-formed.
_REVIEWER_ADDR_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+$")


@bp_web.route("/<inbox_name>/search")
def search(inbox_name: str):
    """Substring search over subject and author within one inbox.
    GET ?q=<query>; renders the search form even on no-result so the
    user always lands on a page with the input."""
    raw_q = request.args.get("q", "").strip()
    q = raw_q[:SEARCH_QUERY_MAX_LEN]

    too_short = 0 < len(q) < SEARCH_QUERY_MIN_LEN
    results: list = []
    truncated = False
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        if len(q) >= SEARCH_QUERY_MIN_LEN:
            results = search_articles(session, inbox, q, limit=SEARCH_RESULT_CAP)
            truncated = len(results) >= SEARCH_RESULT_CAP
        lifecycle_status_by_id = lifecycle_status_for_articles(
            session, [a.id for a in results]
        )

    # SearchResultsPage only when we're actually rendering results.
    # The no-query and too-short shapes are a bare search form, not a
    # "results page", emitting the type would give crawlers a wrong
    # signal. Canonical URL is `_site_base() + /<inbox>/search` (no
    # query), matching the <link rel="canonical"> the context
    # processor emits, keeps individual ?q= URLs out of the index.
    page_json_ld = None
    if results:
        canonical_url = _site_base() + f"/{inbox.name}/search"
        page_json_ld = _json_ld_search(
            _site_base(),
            inbox.name,
            q,
            canonical_url,
        )

    return render_template(
        "search.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        query=q,
        results=results,
        truncated=truncated,
        result_cap=SEARCH_RESULT_CAP,
        too_short=too_short,
        min_len=SEARCH_QUERY_MIN_LEN,
        max_len=SEARCH_QUERY_MAX_LEN,
        page_json_ld=page_json_ld,
        lifecycle_status_by_id=lifecycle_status_by_id,
    )


@bp_web.route("/<inbox_name>/author/<sub>")
def author_view(inbox_name: str, sub: str):
    """Chronological view of recent messages from one author within an
    inbox. Reuses `author_recent` (cached); shows up to
    AUTHOR_VIEW_LIMIT most-recent matches."""
    sub = sub.strip()[:SEARCH_QUERY_MAX_LEN]
    if len(sub) < SEARCH_QUERY_MIN_LEN:
        abort(404)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        results = author_recent(session, inbox, sub, limit=AUTHOR_VIEW_LIMIT)
        lifecycle_status_by_id = lifecycle_status_for_articles(
            session, [a.id for a in results]
        )
    # Pin the canonical URL with `sub` percent-encoded so the page's
    # <link rel="canonical"> matches the <link rel="alternate"> for
    # the atom feed (which uses Jinja's `urlencode` filter on `sub`).
    # `request.path` would surface raw `@` for queries like
    # `torvalds@`; the encoded form is standards-conformant and keeps
    # the two surfaces consistent (2026-05-13 review nit).
    base = _site_base()
    sub_quoted = quote(sub, safe="")
    canonical_url = f"{base}/{inbox.name}/author/{sub_quoted}"
    return render_template(
        "author.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        sub=sub,
        results=results,
        truncated=len(results) >= AUTHOR_VIEW_LIMIT,
        result_cap=AUTHOR_VIEW_LIMIT,
        canonical_url=canonical_url,
        page_json_ld=_json_ld_author(base, inbox.name, sub, canonical_url),
        lifecycle_status_by_id=lifecycle_status_by_id,
    )


@bp_web.route("/<inbox_name>/reviewer/<path:address>")
def reviewer_view(inbox_name: str, address: str):
    """Per-reviewer attestation listing: every patch in `inbox_name`
    where `address` appears on a `Reviewed-by` / `Acked-by` /
    `Tested-by` / `Reported-by` / `Suggested-by` /
    `Co-developed-by` / `Reported-and-tested-by` trailer.

    The address from the URL is lowercased to match the
    `address_normalized` index column. We accept any well-formed
    address in the URL (the route is a public navigation surface),
    but outbound links to this surface are only generated from
    allowlisted addresses (see `_is_allowlisted_address_filter`).
    Non-allowlisted addresses can be navigated to directly by anyone
    who already knows them, matching the existing posture that
    redaction is friction, not a privacy guarantee.
    """
    if not _REVIEWER_ADDR_RE.match(address):
        abort(404)
    address_normalized = address.lower()
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        entries = articles_reviewed_by(
            session,
            inbox,
            address_normalized,
            limit=REVIEWS_PER_PAGE_LIMIT,
        )
        lifecycle_status_by_id = lifecycle_status_for_articles(
            session, [e.article_id for e in entries]
        )
    role_counts: dict[str, int] = {}
    for e in entries:
        role_counts[e.role] = role_counts.get(e.role, 0) + 1
    base = _site_base()
    canonical_url = (
        f"{base}/{inbox.name}/reviewer/{quote(address_normalized, safe='@')}"
    )
    return render_template(
        "reviewer.html",
        inbox_name=inbox.name,
        current_inbox=inbox.name,
        address=address_normalized,
        entries=entries,
        role_counts=role_counts,
        total=len(entries),
        truncated=len(entries) >= REVIEWS_PER_PAGE_LIMIT,
        result_cap=REVIEWS_PER_PAGE_LIMIT,
        canonical_url=canonical_url,
        lifecycle_status_by_id=lifecycle_status_by_id,
    )
