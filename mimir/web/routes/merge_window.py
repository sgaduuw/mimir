"""`/merge-window`: patches in linux-next that Linus's tree does not
have yet, grouped by kernel subsystem (#672, #703). Data in
`mimir.merge_window`.
"""

import hashlib

from flask import Response, render_template, request

from mimir.extensions import SessionLocal
from mimir.linus_pulls import linus_pulls, linus_pulls_state
from mimir.merge_window import (
    MERGE_WINDOW_PATH,
    SECTION_MIN_PATCHES,
    merge_window,
    merge_window_state,
)
from mimir.web._blueprint import bp_web
from mimir.web.urls import _site_base


@bp_web.route(MERGE_WINDOW_PATH)
def merge_window_page():
    """`public, no-cache` like the message pages: the ETag comes from
    the same state strings as the two caches' keys (the linux-next
    queue and the pull requests to Linus), so a finished walk or a new
    pull request moves the ETag, and a revalidation that matches costs
    a few small queries."""
    # The canonical follows `SITE_BASE_URL`, a knob a restart can change
    # without a version bump, so it is part of the ETag (as on the message
    # page). The data cache key leaves it out: the data does not use it.
    canonical_url = _site_base() + MERGE_WINDOW_PATH
    with SessionLocal() as session:
        state = merge_window_state(session)
        pulls_state = linus_pulls_state(session)
        etag = hashlib.blake2s(
            f"{state}|{pulls_state}|{canonical_url}".encode(), digest_size=8
        ).hexdigest()
        if etag in request.if_none_match:
            response = Response(status=304)
            response.set_etag(etag)
            return response
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
        )
    )
    response.set_etag(etag)
    return response
