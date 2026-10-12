"""`/merge-window`: patches in linux-next that Linus's tree does not
have yet, grouped by kernel subsystem (#672, #703). Data in
`mimir.merge_window`.
"""

import hashlib

from flask import Response, render_template, request

from mimir.extensions import SessionLocal
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
    the same state string as the data's cache key, so a finished
    linux-next or Linus walk moves both, and a revalidation that
    matches costs one small query."""
    # The canonical follows `SITE_BASE_URL`, a knob a restart can change
    # without a version bump, so it is part of the ETag (as on the message
    # page). The data cache key leaves it out: the data does not use it.
    canonical_url = _site_base() + MERGE_WINDOW_PATH
    with SessionLocal() as session:
        state = merge_window_state(session)
        etag = hashlib.blake2s(
            f"{state}|{canonical_url}".encode(), digest_size=8
        ).hexdigest()
        if etag in request.if_none_match:
            response = Response(status=304)
            response.set_etag(etag)
            return response
        data = merge_window(session, state)
    response = Response(
        render_template(
            "merge_window.html",
            mw=data,
            current_inbox=None,
            canonical_url=canonical_url,
            section_min=SECTION_MIN_PATCHES,
        )
    )
    response.set_etag(etag)
    return response
