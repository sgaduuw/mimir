"""Static-ish meta endpoints: `robots.txt`, `security.txt`,
`favicon.svg`, `og-image.png`.

The OG image constants (`OG_IMAGE_*`) live here because they're
referenced from both the route itself and the template-globals
context processor in `hooks.py`; importing from this module avoids
duplicating the values.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Response, abort, render_template, send_from_directory

from mimir.config import settings
from mimir.web._blueprint import bp_web
from mimir.web.urls import _site_base


# OG image: 1200x630 PNG composited from a 17th-century Icelandic
# Edda manuscript depiction of Ratatoskr (AM 738 4to era, Árni
# Magnússon Institute, public domain). Twitter/X doesn't render SVG
# and LinkedIn is inconsistent on it; PNG is the safer baseline. The
# asset is pre-baked by `bake_og_image.py` and checked in under
# `static/img/`; this route exists to keep the URL at the site root
# (matches the prior `/og-image.svg` shape and reads as deliberate
# branding rather than a deep static-folder path).
OG_IMAGE_FILENAME = "og-image.png"
OG_IMAGE_WIDTH = 1200
OG_IMAGE_HEIGHT = 630
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
def robots():
    """Static robots.txt, disallows attachment downloads (saves bot
    bandwidth on binaries) and points crawlers at the sitemap."""
    sitemap_url = _site_base() + "/sitemap.xml"
    body = render_template("robots.txt", sitemap_url=sitemap_url)
    return Response(body, mimetype="text/plain; charset=utf-8")


@bp_web.route("/favicon.svg")
def favicon_svg():
    return Response(_FAVICON_SVG, mimetype="image/svg+xml")


@bp_web.route("/og-image.png")
def og_image_png():
    return send_from_directory(_STATIC_IMG_DIR, OG_IMAGE_FILENAME, mimetype="image/png")


@bp_web.route("/security.txt")
@bp_web.route("/.well-known/security.txt")
def security_txt():
    """RFC 9116 security.txt. 404 unless `SECURITY_CONTACT` is set
    don't ship a contact-less file. The Expires field is computed at
    request time as `now + 1 year` so it never falls into the past."""
    if not settings.security_contact:
        abort(404)
    expires = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat(timespec="seconds")
    body = render_template(
        "security.txt",
        contact=settings.security_contact,
        expires=expires,
        preferred_languages=settings.security_preferred_languages,
        policy_url=settings.security_policy_url,
        encryption_url=settings.security_encryption_url,
    )
    return Response(body, mimetype="text/plain; charset=utf-8")
