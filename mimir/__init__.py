from importlib.metadata import PackageNotFoundError, version as _pkg_version

# Runtime version, read from the installed package metadata (poetry /
# pip install). The sentinel covers a source-tree-only checkout where
# `mimir` isn't installed; in that mode the footer just shows
# "0.0.0+unknown" instead of crashing.
try:
    __version__ = _pkg_version("mimir")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

from flask import Flask  # noqa: E402
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

from mimir.cli import register_cli  # noqa: E402
from mimir.config import settings  # noqa: E402
from mimir.inboxes import bootstrap_inboxes  # noqa: E402
from mimir.web import bp_web  # noqa: E402


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = settings.secret_key
    app.config["DEBUG"] = settings.flask_debug

    # Honour X-Forwarded-* headers when running behind a known number of
    # trusted reverse-proxy hops, so request.remote_addr / .scheme /
    # .host reflect the real client. Off by default — enabling this when
    # the app is reachable directly would let anyone spoof those values
    # via a forged XFF header.
    if settings.trusted_proxy_hops > 0:
        n = settings.trusted_proxy_hops
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)

    app.register_blueprint(bp_web)
    register_cli(app)
    bootstrap_inboxes()
    return app
