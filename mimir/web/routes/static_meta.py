"""Static-ish meta endpoints: `robots.txt`, `llms.txt`, `security.txt`,
`favicon.svg`, `favicon.ico`, `og-image.png`, `privacy`.

The OG image constants (`OG_IMAGE_*`) live here because they're
referenced from both the route itself and the template-globals
context processor in `hooks.py`; importing from this module avoids
duplicating the values.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from flask import (
    Response,
    abort,
    redirect,
    render_template,
    send_from_directory,
    url_for,
)

from mimir import robots
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.web._blueprint import bp_web
from mimir.web.urls import _site_base, markdown_path

# OG image: 1200x630 PNG composited from a 17th-century Icelandic
# Edda manuscript depiction of Ratatoskr (AM 738 4to era, Árni
# Magnússon Institute, public domain). Twitter/X doesn't render SVG
# and LinkedIn is inconsistent on it; PNG is the safer baseline. The
# asset is pre-baked by `bake_og_image.py` and checked in under
# `static/img/`; this route exists to keep the URL at the site root
# (matches the prior `/og-image.svg` shape and reads as deliberate
# branding rather than a deep static-folder path).
OG_IMAGE_FILENAME = "og-image.png"
OG_IMAGE_ALT = (
    "Ratatoskr from a 17th-century Icelandic Edda manuscript "
    "(AM 738 4to era, Árni Magnússon Institute, public domain), "
    "next to the ratatoskr.run wordmark."
)


# Static-img dir resolves from the package root (mimir/), one level up
# from this routes/ subpackage.
_STATIC_IMG_DIR = Path(__file__).resolve().parents[2] / "static" / "img"


# Placeholder squirrel-adjacent emoji until a proper logo lands
# (tracked in CONTEXT.md "Roadmap, Favicon / logo"). Inline SVG keeps
# the file at a couple of hundred bytes and avoids a static-folder
# dependency. Browsers cache the response aggressively per the
# `_CACHE_CONTROL_BY_ENDPOINT` map.
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<text x="0" y="14" font-size="14">🐿️</text></svg>'
)


@bp_web.route("/robots.txt")
def robots_txt():
    """DB-backed robots.txt. Rows in `robots_rules` produce one
    stanza each; the `*` row is seeded by the migration with the
    same values the previous hardcoded template used. Operator
    mutates via `admin robots {add,update,remove,reset}`."""
    # Both indexes (#964): pages mimir adds, then message pages.
    base = _site_base()
    with SessionLocal() as session:
        body = robots.render_robots_txt(
            session, [f"{base}/sitemap.xml", f"{base}/messages-sitemap.xml"]
        )
    return Response(body, mimetype="text/plain")


@bp_web.route("/favicon.svg")
def favicon_svg():
    return Response(_FAVICON_SVG, mimetype="image/svg+xml")


@bp_web.route("/favicon.ico")
def favicon_ico():
    """Browsers and link-preview tools still ask for /favicon.ico. Without
    this route it falls through to the inbox route and two-hops to a 404
    (#792). The icon is an emoji, so a baked ICO would mean shipping emoji
    artwork; the SVG renders in the clients that ask."""
    return redirect(url_for("web.favicon_svg"), code=301)


@bp_web.route("/og-image.png")
def og_image_png():
    return send_from_directory(_STATIC_IMG_DIR, OG_IMAGE_FILENAME, mimetype="image/png")


@bp_web.route("/privacy")
def privacy():
    """GDPR Art. 13 transparency notice. Single static-shaped page
    rendered into `base.html`; no per-request data, so the route is
    a thin `render_template` call. Cache-Control comes from the
    `_CACHE_CONTROL_BY_ENDPOINT` entry for `web.privacy`."""
    return render_template("privacy.html")


@bp_web.route("/security.txt")
@bp_web.route("/.well-known/security.txt")
def security_txt():
    """RFC 9116 security.txt. 404 unless `SECURITY_CONTACT` is set
    don't ship a contact-less file. The Expires field is computed at
    request time as `now + 1 year` so it never falls into the past."""
    if not settings.security_contact:
        abort(404)
    expires = (datetime.now(UTC) + timedelta(days=365)).isoformat(timespec="seconds")
    body = render_template(
        "security.txt",
        contact=settings.security_contact,
        expires=expires,
        preferred_languages=settings.security_preferred_languages,
        policy_url=settings.security_policy_url,
        encryption_url=settings.security_encryption_url,
    )
    return Response(body, mimetype="text/plain")


@bp_web.route("/llms.txt")
def llms_txt():
    """What the site is and where its Markdown pages are, for AI agents
    (#884, https://llmstxt.org/). The `*` stanza's Content-Signal is
    copied from robots.txt through the same functions, so an invitation
    to fetch the Markdown pages never contradicts what robots.txt says
    agents may do with them."""
    with SessionLocal() as session:
        rule = robots.get_rule(session, "*")
        signals = rule.content_signals if rule is not None else None
    message = "/<inbox>/<YYYY>/<MM>/<id>"
    shapes = [
        ("A message", message),
        ("A whole thread, first page", message + "/t"),
        ("A later thread page", message + "/t/<page>"),
    ]
    body = render_template(
        "llms.txt",
        content_signal=robots.format_content_signals(signals) if signals else None,
        shapes=[(label, path, markdown_path(path)) for label, path in shapes],
    )
    # Jinja drops a template's final newline; a text file ends with one.
    return Response(body + "\n", mimetype="text/plain")
