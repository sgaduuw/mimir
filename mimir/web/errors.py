"""Branded 4xx/5xx error pages.

Werkzeug's default error pages are plain-text fragments without the
site shell, nav, footer, or even the security headers (well, the
hooks do apply, but the body has no chrome). Crawlers, link-card
previews, and accidental typos in shared URLs all land here, so
the page should match the rest of the site.

The companion `before_app_request` / `after_app_request` hooks in
`hooks.py` already fire for these responses (pinned by
`test_security_headers_present_on_unmatched_404`), so CSP / HSTS /
Permissions-Policy / X-Frame-Options / X-Content-Type-Options /
Referrer-Policy / Cache-Control / X-Request-Id all carry over for
free. This module adds the only thing missing: the rendered HTML
body.

`app_errorhandler` (not the blueprint-scoped `errorhandler`) so the
handlers fire for every error the app produces, including unmatched-
route 404s that don't even hit the blueprint. `noindex=True` is
passed into the template so `base.html` skips the canonical link
(a canonical pointing at the error URL would tell search engines
"this 404 is authoritative") and emits `<meta name="robots"
content="noindex">` so crawlers don't index typo URLs.
"""
from flask import render_template

from mimir.web._blueprint import bp_web


def _render_error(status: int, title: str, detail: str) -> tuple:
    return (
        render_template(
            "error.html",
            status=status,
            title=title,
            detail=detail,
            noindex=True,
            # Inbox-scoped error pages would normally inherit
            # `current_inbox` from the route view that aborted; on a
            # pure unmatched-route 404 there is no `current_inbox`,
            # and the template's nav uses the meta-index variant by
            # default. Leaving it implicit keeps the contract simple.
        ),
        status,
    )


@bp_web.app_errorhandler(404)
def _not_found(_e):
    return _render_error(
        404,
        "Not found",
        "The page you requested isn't in this archive. "
        "It may have been deleted, never existed, or the URL is "
        "from a stale link.",
    )


@bp_web.app_errorhandler(410)
def _gone(_e):
    return _render_error(
        410,
        "Gone",
        "The page you requested has been deliberately removed and "
        "isn't coming back.",
    )


@bp_web.app_errorhandler(500)
def _server_error(_e):
    return _render_error(
        500,
        "Server error",
        "Something went wrong on the server. The operator has been "
        "logged; try again in a moment.",
    )
