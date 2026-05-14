from importlib.metadata import PackageNotFoundError, version as _pkg_version

# Runtime version, read from the installed package metadata (poetry /
# pip install). The sentinel covers a source-tree-only checkout where
# `mimir` isn't installed; in that mode the footer just shows
# "0.0.0+unknown" instead of crashing.
try:
    __version__ = _pkg_version("mimir")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

from flask import Flask, Response  # noqa: E402
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

from mimir.cli import register_cli  # noqa: E402
from mimir.config import settings  # noqa: E402
from mimir.inboxes import bootstrap_inboxes  # noqa: E402
from mimir.web import bp_web  # noqa: E402


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = settings.secret_key
    app.config["DEBUG"] = settings.flask_debug
    # Strip the whitespace that un-rendered `{% if %}` / `{% for %}`
    # blocks leave behind. Visual difference is cosmetic, but per the
    # 2026-05-11 review the raw HTML showed long runs of blank lines
    # that read as sloppy in view-source.
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True

    # Cache /static/* assets (thread-fold.js, future logos, etc.) for
    # one day. Flask's default is `no-cache`, which made every page
    # load re-fetch the JS controller and any served images. The
    # routed assets (favicon, og-image) carry their own Cache-Control
    # entries via the per-endpoint map in mimir.web; this default
    # covers everything served by Flask's built-in static blueprint,
    # which the per-endpoint map can't reach (different blueprint).
    # 1 day balances bandwidth savings against deploy-cycle
    # propagation: a JS bug fix lands within 24 hours rather than the
    # week the routed-asset entries use.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400

    # Honour X-Forwarded-* headers when running behind a known number of
    # trusted reverse-proxy hops, so request.remote_addr / .scheme /
    # .host reflect the real client. Off by default — enabling this when
    # the app is reachable directly would let anyone spoof those values
    # via a forged XFF header.
    if settings.trusted_proxy_hops > 0:
        n = settings.trusted_proxy_hops
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)

    app.register_blueprint(bp_web)
    _register_indexnow_key_route(app)
    register_cli(app)
    bootstrap_inboxes()
    return app


def _register_indexnow_key_route(app: Flask) -> None:
    """Serve the IndexNow ownership-verification file at /<key>.txt
    when (and only when) `settings.indexnow_key` is set. The path
    contains the literal key value — that's how IndexNow proves the
    submitter owns the host. Registered dynamically because the URL
    is config-dependent; an unconfigured deploy doesn't expose the
    endpoint at all."""
    key = settings.indexnow_key
    if not key:
        return

    def _serve():
        # Spec says the body must be exactly the key. Trailing newline
        # is tolerated by every consumer but we omit it for byte-equal
        # parity with the value the operator set.
        return Response(key, mimetype="text/plain; charset=utf-8")

    app.add_url_rule(f"/{key}.txt", "indexnow_key_file", _serve)
