"""Recover alternate spellings without duplicating article identity rules."""

from datetime import date
from urllib.parse import quote

from flask import abort, redirect

from mimir.web._blueprint import bp_web
from mimir.web.urls import _msg_path, thread_page_url


@bp_web.route(
    "/<inbox_name>/<int:year>-<int:month>-<int:article_id>",
    defaults={"thread": False},
)
@bp_web.route(
    "/<inbox_name>/<int:year>-<int:month>-<int:article_id>/t",
    defaults={"thread": True},
)
def hyphen_date_redirect(inbox_name, year, month, article_id, thread):
    """The existing acceptor owns article dates and cross-post handling.

    These are recovery inputs only; links emitted by the site keep using
    the slash-form builders. No article lookup is needed for this hop.
    """
    try:
        month_date = date(year, month, 1)
    except ValueError, OverflowError:
        abort(404)
    # Unlike stored inbox slugs, this recovery input has not been validated.
    target = (thread_page_url if thread else _msg_path)(
        article_id, month_date, quote(inbox_name, safe="")
    )
    return redirect(target, code=301)
