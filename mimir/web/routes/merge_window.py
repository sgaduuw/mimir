"""`/merge-window`: patches in linux-next that Linus's tree does not
have yet, grouped by kernel subsystem (#672, #703). Data in
`mimir.merge_window`.
"""

import hashlib

from flask import Response, abort, get_template_attribute, render_template, request

from mimir.extensions import SessionLocal
from mimir.linus_pulls import linus_pulls, linus_pulls_state
from mimir.merge_window import (
    MERGE_WINDOW_PATH,
    SECTION_MIN_PATCHES,
    SECTION_ROW_CAP,
    merge_window,
    merge_window_state,
)
from mimir.web._blueprint import bp_web
from mimir.web.routes._validators import allowlist_tag
from mimir.web.urls import _site_base


def _etag(state: str, pulls_state: str, canonical_url: str, view: str) -> str:
    # The canonical follows `SITE_BASE_URL`, a knob a restart can change
    # without a version bump, so it is part of the ETag (as on the message
    # page). The data cache key leaves it out: the data does not use it.
    # The author lines name a nameless allowlisted sender by its address
    # (`display_name`), so the allowlist is an input too. The row cap is a
    # code constant, so the version in `state` covers it. `view` names the
    # page and its representation, so a subsystem page, its HTMX rows and
    # the main page never share a tag.
    return hashlib.blake2s(
        f"{state}|{pulls_state}|{canonical_url}|{allowlist_tag()}|{view}".encode(),
        digest_size=8,
    ).hexdigest()


def _not_modified(etag: str) -> Response | None:
    if request.if_none_match.contains_weak(etag):
        response = Response(status=304)
        response.set_etag(etag)
        return response
    return None


@bp_web.route(MERGE_WINDOW_PATH)
def merge_window_page():
    """`public, no-cache` like the message pages: the ETag comes from
    the same state strings as the two caches' keys (the linux-next
    queue and the pull requests to Linus), so a finished walk or a new
    pull request moves the ETag, and a revalidation that matches costs
    a few small queries."""
    canonical_url = _site_base() + MERGE_WINDOW_PATH
    with SessionLocal() as session:
        state = merge_window_state(session)
        pulls_state = linus_pulls_state(session)
        etag = _etag(state, pulls_state, canonical_url, "main")
        if (cached := _not_modified(etag)) is not None:
            return cached
        data = merge_window(session, state)
        pulls = linus_pulls(session, pulls_state)
    response = Response(
        render_template(
            "merge_window.html",
            mw=data,
            pulls=pulls,
            current_inbox=None,
            canonical_url=canonical_url,
            section_min=SECTION_MIN_PATCHES,
            row_cap=SECTION_ROW_CAP,
            focus=None,
        )
    )
    response.set_etag(etag)
    return response


@bp_web.route(f"{MERGE_WINDOW_PATH}/subsystems/<anchor>")
def merge_window_subsystem(anchor: str):
    """One subsystem's queued postings, all of them (#1259). The main page
    lists a larger subsystem's newest `SECTION_ROW_CAP` and links here for
    the rest; a smaller subsystem's disclosure holds only a link here.
    Under `HX-Request: true` the answer is the rows alone, which those
    links swap in place; otherwise the full page, noindex with no
    canonical like a filtered month view: its rows are a subset of the
    main page's, and its links stay followable. Both representations
    share this URL, so `web.hooks` sets `Vary: HX-Request` for it."""
    canonical_url = _site_base() + MERGE_WINDOW_PATH
    hx_request = request.headers.get("HX-Request") == "true"
    with SessionLocal() as session:
        state = merge_window_state(session)
        pulls_state = linus_pulls_state(session)
        view = f"{anchor}|{'hx' if hx_request else 'full'}"
        etag = _etag(state, pulls_state, canonical_url, view)
        if (cached := _not_modified(etag)) is not None:
            return cached
        data = merge_window(session, state)
    focus = next((s for s in data.sections + data.smaller if s.anchor == anchor), None)
    if focus is None or not focus.rows:
        abort(404)
    if hx_request:
        body = get_template_attribute("_mw_rows.html", "rows")(focus.rows)
    else:
        body = render_template(
            "merge_window.html",
            mw=data,
            pulls=None,
            current_inbox=None,
            canonical_url=canonical_url,
            section_min=SECTION_MIN_PATCHES,
            row_cap=None,
            focus=focus,
            noindex=True,
        )
    response = Response(body)
    response.set_etag(etag)
    return response
