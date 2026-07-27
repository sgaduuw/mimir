"""Per-request hooks (timer, request-id mint, JSON access log,
Cache-Control + security headers) and the one app-context processor
that injects site-wide template globals.

`_CACHE_CONTROL_BY_ENDPOINT` is the per-endpoint cache rule table the
`_add_cache_headers` after-request hook applies; `_SECURITY_HEADERS`
is the defense-in-depth set every response gets.

`before_app_request` / `after_app_request` (not the blueprint-scoped
forms) so unmatched-route 404s still go through the same hooks; the
audit (2026-05-15) flagged that blueprint-scoped hooks let those
404s bypass security headers and the structured log.
"""

import json
import logging
import secrets
import time
from datetime import datetime, timezone

from flask import g, request

from mimir.config import settings
from mimir.inboxes import inbox_names
from mimir.web._blueprint import bp_web
from mimir.web.routes.static_meta import OG_IMAGE_ALT
from mimir.web.urls import _site_base


@bp_web.app_context_processor
def _inject_template_globals() -> dict:
    """Inboxes are needed by base.html for the nav. `current_inbox` is set
    per-view (None on the meta-index `/`). Names come from the cached
    list populated at bootstrap, no per-request DB hit. `site_name` is
    the configurable brand; "mimir" stays as the page generator.

    `default_canonical_url` is the https-safe fallback for og:url on
    routes that don't pin their own canonical (search, daily, year,
    month, author …). Built from `_site_base()` so SITE_BASE_URL
    forces correctness even when ProxyFix isn't in scope.
    """
    from flask import has_request_context

    from mimir import __version__ as mimir_version

    site_base = ""
    default_canonical = ""
    if has_request_context():
        site_base = _site_base()
        default_canonical = site_base + request.path
    return {
        "inboxes": inbox_names(),
        "site_name": settings.site_name,
        "mimir_version": mimir_version,
        "site_base": site_base,
        "default_canonical_url": default_canonical,
        "og_image_alt": OG_IMAGE_ALT,
        # `noindex` is True on error pages so base.html suppresses
        # the canonical link + emits a `<meta name="robots"
        # content="noindex">` (a canonical pointing at the error URL
        # would tell search engines "this 404 is authoritative,"
        # which is exactly the wrong signal). Default False keeps
        # every other route's canonical contract unchanged.
        "noindex": False,
    }


# Cache-Control per endpoint. Lets edge caches (Cloudflare, an nginx in
# front, the browser) absorb scraper traffic on the high-volume endpoints
# without pinning stale content for too long. Attachments are
# byte-stable per (message_id, n), so they're cached aggressively;
# listings are cached briefly so new messages don't take more than ~1
# minute to surface; pagination and 404s/redirects skip caching.
_CACHE_CONTROL_BY_ENDPOINT = {
    "web.index": "public, max-age=300",
    "web.inbox_dashboard": "public, max-age=60",
    "web.daily_today": "public, max-age=60",
    "web.daily_yesterday": "public, max-age=600",
    "web.threads_since_view": "public, max-age=600",
    "web.subsystem_dashboard": "public, max-age=600",
    "web.year_archive": "public, max-age=600",
    "web.month_archive": "public, max-age=600",
    "web.search": "public, max-age=300",
    "web.author_view": "public, max-age=300",
    "web.reviewer_view": "public, max-age=600",
    "web.inbox_feed": "public, max-age=300",
    "web.author_feed": "public, max-age=300",
    # /robots.txt is now operator-tunable at runtime (see
    # `admin robots`). Keep the edge cache short so an
    # `admin robots add GPTBot --disallow /` propagates within
    # minutes rather than a day. Matches the sitemap.xml cadence
    # for the same reason (the underlying state is mutable).
    "web.robots_txt": "public, max-age=300",
    "web.security_txt": "public, max-age=3600",
    "web.privacy": "public, max-age=3600",
    "web.favicon_svg": "public, max-age=604800",
    "web.og_image_png": "public, max-age=604800",
    "web.sitemap": "public, max-age=300",
    "web.meta_sitemap": "public, max-age=300",
    "web.inbox_sitemap": "public, max-age=300",
    "web.maintainers_sitemap": "public, max-age=300",
    "web.message_id_lookup": "public, max-age=3600",
    "web.message_id_lookup_inbox": "public, max-age=3600",
    # web.message uses ETag-based conditional revalidation (set inside
    # the route handler). `no-cache` directs browsers + edges to ALWAYS
    # revalidate via If-None-Match before reusing a cached body; matched
    # ETags resolve as 304 with no body, so the bandwidth cost is small
    # while the within-window stale-after-deploy problem (a code change
    # leaving cached pages mis-rendered up to max-age) goes away.
    "web.message": "public, no-cache",
    # Same ETag-revalidation posture as web.message, and the payoff is
    # larger here: a 304 skips up to `thread_view_render_cap` git blob
    # fetches and parses, which is the whole cost of this page.
    "web.thread_view": "public, no-cache",
    "web.attachment_download": "public, max-age=3600, immutable",
    "web.attachment_preview": "public, max-age=3600, immutable",
}


# Defense-in-depth response headers. Applied to every response so 404s
# and error pages also get them.
#
# CSP: HTML escaping is correct, but a CSP narrows the blast radius of
#   any future bug. `default-src 'self'` plus the two CDNs we SRI-pin.
#   `style-src` no longer carries `'unsafe-inline'`: every inline
#   `<style>` block moved to `mimir/static/css/mimir.css` under #228,
#   and every per-element `style="..."` attribute moved to a CSS class
#   in the security pass (the thread-tree depth ladder is enumerated
#   as `data-depth="N"` rules so a dynamic value still works). Any
#   regression that re-introduces inline styles will fail to render
#   instead of silently widening the attack surface.
#   `script-src` is pinned to the specific htmx version path
#   (`unpkg.com/htmx.org@1.9.12/`); an htmx bump in `base.html` must
#   update this CSP entry in lockstep, the test
#   `test_csp_script_src_pins_specific_htmx_version` enforces that.
#   Pygments is configured `noclasses=False` (both inline-renderer in
#   `mimir/rendering.py` and attachment-preview in
#   `mimir/web/routes/attachments.py`) so it emits class names, not
#   inline `style="color:..."`.
# Permissions-Policy: deny every powerful feature mimir doesn't use,
#   the page is a read-only archive browser, none of the listed
#   features have a legitimate use case here. Empty allowlist `()`
#   on every directive means "deny in both this document and any
#   embedded subframe."
# Referrer-Policy: don't leak full URLs (which include Message-IDs and
#   inbox names) to outbound links.
# X-Content-Type-Options: forces browsers to honor the Content-Type we
#   send rather than sniffing.
# X-Frame-Options: trivial anti-clickjacking. mimir has no embed use
#   case.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "style-src 'self' https://cdn.jsdelivr.net; "
        "script-src 'self' https://unpkg.com/htmx.org@1.9.12/; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    ),
    "Permissions-Policy": (
        "accelerometer=(), "
        "ambient-light-sensor=(), "
        "autoplay=(), "
        "battery=(), "
        "camera=(), "
        "display-capture=(), "
        "document-domain=(), "
        "encrypted-media=(), "
        "fullscreen=(), "
        "geolocation=(), "
        "gyroscope=(), "
        "keyboard-map=(), "
        "magnetometer=(), "
        "microphone=(), "
        "midi=(), "
        "payment=(), "
        "picture-in-picture=(), "
        "publickey-credentials-get=(), "
        "screen-wake-lock=(), "
        "sync-xhr=(), "
        "usb=(), "
        "web-share=(), "
        "xr-spatial-tracking=()"
    ),
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


# One JSON object per request to stdout. Doesn't propagate to root,
# so it doesn't double-log via app.logger if anyone reconfigures it.
_request_logger = logging.getLogger("mimir.request")
_request_logger.propagate = False
if not _request_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    _request_logger.addHandler(_h)
    _request_logger.setLevel(logging.INFO)


@bp_web.before_app_request
def _start_request_timer():
    # `before_app_request` (not `before_request`) so this fires for
    # EVERY request the app handles, not just routes that matched
    # this blueprint. A URL with no matching pattern (Flask's
    # built-in 404) still needs `g._request_id` populated for the
    # access-log line and for `_add_cache_headers` to set
    # `X-Request-Id` on the response. The audit (2026-05-15) flagged
    # the blueprint-scoped form as letting unmatched-route 404s
    # bypass security headers, cache rules, and the structured log.
    g._request_t0 = time.perf_counter()
    # Honor an upstream-supplied request id (typical reverse-proxy
    # pattern) so multi-hop traces stay correlatable; otherwise mint
    # a fresh short id.
    g._request_id = request.headers.get("X-Request-Id") or secrets.token_hex(8)


@bp_web.after_app_request
def _add_cache_headers(response):
    # 200 OK and 301/302 redirects (Message-ID lookup) are cacheable;
    # 304 Not Modified (web.message conditional revalidation) must
    # carry the same Cache-Control as the would-be 200 per RFC 7232
    # so the client knows when to revalidate again. 4xx/5xx skip,
    # error responses shouldn't be pinned in upstream caches.
    if response.status_code in (200, 301, 302, 304):
        rule = _CACHE_CONTROL_BY_ENDPOINT.get(request.endpoint)
        if rule:
            response.headers["Cache-Control"] = rule
    # The message route returns a partial (`_message_body.html`) under
    # `HX-Request: true` and the full page otherwise. Without Vary,
    # caches (browser bfcache, Cloudflare, Chrome's prerender cache
    # for sites with speculation rules) can serve a full-page response
    # to an HTMX request, which then swaps the entire <body>'s
    # children into `#msg` and visibly duplicates the page chrome.
    # The Vary header keys the two response variants separately.
    if request.endpoint == "web.message":
        response.headers["Vary"] = "HX-Request"
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    # HSTS only when we know the request came in over HTTPS. We gate
    # on `request.is_secure` rather than the raw `X-Forwarded-Proto`
    # header so the trust is consistent with `TRUSTED_PROXY_HOPS`:
    # with ProxyFix wired up, `is_secure` honours the trusted
    # forwarded header; with it unwired (the default), `is_secure`
    # falls back to the actual connection scheme and a forged header
    # can't pin HSTS into a casual browser cache. An http://localhost
    # dev session stays HSTS-less; the production path through Caddy
    # (with ProxyFix=2) sets is_secure True and gets the header.
    # `preload` opts the site into the browser-bundled HSTS preload
    # list once submitted via hstspreload.org; submission is a one-way
    # door (un-preloading takes months), and the site is HTTPS-only
    # via Caddy + Tailscale Funnel so the commitment is consistent
    # with the current posture. The directive alone doesn't auto-
    # submit; it signals readiness.
    if request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains; preload",
        )
    response.headers.setdefault("X-Request-Id", getattr(g, "_request_id", "-"))
    return response


@bp_web.after_app_request
def _log_request(response):
    """Emit one JSON-line access record per request. Runs after the
    cache + security headers are set so duration covers the full
    response-build path."""
    t0 = getattr(g, "_request_t0", None)
    duration_ms = round((time.perf_counter() - t0) * 1000, 1) if t0 else None
    _request_logger.info(
        json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "request_id": getattr(g, "_request_id", None),
                "method": request.method,
                "path": request.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "remote": request.remote_addr,
                # Read the header directly: Werkzeug's request.user_agent is a
                # UserAgent wrapper whose __bool__ depends on the bundled UA
                # parser detecting a known browser, which makes legitimate
                # values like "curl/8.20.0", and, with this Werkzeug, plain
                # Firefox, evaluate falsy and silently turn into null. The
                # raw header is what we actually want to log.
                "ua": request.headers.get("User-Agent"),
                "referrer": request.referrer,
            }
        )
    )
    return response
