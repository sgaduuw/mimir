"""HTMX backend endpoints.

Currently just `/api/<inbox>/recent` for the dashboard's "Load more"
trigger; lives separately so adding more HTMX endpoints doesn't grow
the route-rich `dashboards.py` further.
"""

from flask import abort, render_template, request

from mimir.extensions import SessionLocal
from mimir.web._blueprint import bp_web
from mimir.web.routes.dashboards import RECENT_PAGE_SIZE, _fetch_recent
from mimir.web.urls import _get_inbox_or_404

# Hard ceiling on `?offset=` for the load-more endpoint. SQLite's
# OFFSET walks `offset` rows on the date index before returning
# anything, so an unbounded query lets a crawler at `?offset=5000000`
# burn a gunicorn worker walking 5 M index entries per request.
# 100 pages back is far past any realistic "Load more" click rate
# (a real reader would have scrolled away long before). Past the
# cap we 404, which HTMX's `hx-swap-oob` handles cleanly. A future
# cursor-pagination shape (`?after_article_id=...`) would obviate
# the ceiling and also fix the duplicates-on-new-ingest gap that
# OFFSET pagination always has; punted as out of scope here.
_MAX_RECENT_OFFSET = RECENT_PAGE_SIZE * 100


@bp_web.route("/api/<inbox_name>/recent")
def api_recent(inbox_name: str):
    """HTMX load-more endpoint for the Recent messages list, scoped to
    one inbox. Returns the `_recent_items.html` partial: the next page
    of <li>s plus a fresh 'Load more' trigger (or nothing, if exhausted)."""
    offset = max(0, request.args.get("offset", default=0, type=int))
    if offset > _MAX_RECENT_OFFSET:
        abort(404)
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        recent, recent_has_more = _fetch_recent(
            session, inbox, offset, RECENT_PAGE_SIZE
        )
    return render_template(
        "_recent_items.html",
        inbox_name=inbox.name,
        recent=recent,
        recent_has_more=recent_has_more,
        recent_next_offset=offset + RECENT_PAGE_SIZE,
    )
