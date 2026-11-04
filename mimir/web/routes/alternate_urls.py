"""Recover alternate spellings without duplicating article identity rules."""

from flask import redirect, url_for

from mimir.web._blueprint import bp_web


@bp_web.route(
    "/<inbox_name>/<int:year>-<int:month>-<int:article_id>",
    defaults={"thread": False},
)
@bp_web.route(
    "/<inbox_name>/<int:year>-<int:month>-<int:article_id>/t",
    defaults={"thread": True},
)
def hyphen_date_redirect(inbox_name, year, month, article_id, thread):
    """The existing acceptor owns date validity and cross-post handling.

    These are recovery inputs only; links emitted by the site keep using
    the slash-form builders. No article lookup is needed for this hop.
    """
    return redirect(
        url_for(
            "web.thread_view" if thread else "web.message",
            inbox_name=inbox_name,
            year=year,
            month=month,
            article_id=article_id,
        ),
        code=301,
    )
