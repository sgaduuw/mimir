from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from mimir.cli import register_cli
from mimir.config import settings
from mimir.inboxes import bootstrap_inboxes
from mimir.web import bp_web


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
