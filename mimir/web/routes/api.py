"""HTMX backend endpoints: `/api/<inbox>/recent` for the dashboard's
"Load more" trigger, and `/api/<inbox>/body/<id>` for the thread view's
"more lines" link.

Lives separately so adding more HTMX endpoints doesn't grow the
route-rich `dashboards.py` further.
"""

import hashlib

from flask import Response, abort, render_template, request
from sqlalchemy import select

import mimir
from mimir import maintainer_allowlist
from mimir.extensions import SessionLocal
from mimir.lifecycle_status import lifecycle_status_for_articles
from mimir.models import Article, ArticleList
from mimir.rendering import render_body
from mimir.store import MessageNotFound, read_message
from mimir.web._blueprint import bp_web
from mimir.web.filters import _redact_trailer_address
from mimir.web.routes._validators import allowlist_tag
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
        lifecycle_status_by_id = lifecycle_status_for_articles(
            session, [a.id for a in recent], inbox.id
        )
    return render_template(
        "_recent_items.html",
        inbox_name=inbox.name,
        recent=recent,
        recent_has_more=recent_has_more,
        recent_next_offset=offset + RECENT_PAGE_SIZE,
        lifecycle_status_by_id=lifecycle_status_by_id,
    )


@bp_web.route("/api/<inbox_name>/body/<int:article_id>")
def api_message_body(inbox_name: str, article_id: int):
    """The thread view's "more lines" fragment: one message's whole body
    in the thread view's compact rendering, uncut.

    404 unless the article is linked to this inbox, the same
    `ArticleList` row `read_message` resolves its blob through.

    The ETag carries every input the output has: the blob (its epoch
    and commit are immutable per inbox and article, so they identify
    the bytes), the render code (`mimir.__version__`), and the two
    address-redaction inputs, the static `email_allowlist` and the
    MAINTAINERS-derived address set. The set is hashed by value because
    it moves on a reparse without a release.
    """
    with SessionLocal() as session:
        inbox = _get_inbox_or_404(session, inbox_name)
        link = session.execute(
            select(Article.message_id, ArticleList.epoch, ArticleList.commit_sha)
            .join(ArticleList, ArticleList.article_id == Article.id)
            .where(Article.id == article_id, ArticleList.inbox_id == inbox.id)
        ).one_or_none()
        if link is None:
            abort(404)
        etag = hashlib.blake2s(
            "|".join(
                (
                    "body",
                    mimir.__version__,
                    link.epoch,
                    link.commit_sha,
                    allowlist_tag(),
                    "\0".join(sorted(maintainer_allowlist.maintainer_addresses())),
                )
            ).encode(),
            digest_size=8,
        ).hexdigest()
        if etag in request.if_none_match:
            response = Response(status=304)
        else:
            try:
                parsed = read_message(session, inbox, link.message_id)
            except MessageNotFound:
                abort(404)
            body = render_body(
                parsed.body,
                address_redactor=_redact_trailer_address,
                compact=True,
            )
            response = Response(f'<div class="message-body">{body}</div>')
    response.set_etag(etag)
    # A fragment is not a page: nothing to index, and /api/ is
    # robots-disallowed already.
    response.headers["X-Robots-Tag"] = "noindex"
    return response
