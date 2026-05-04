from flask import Flask

from mimir.cli import register_cli
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.inboxes import bootstrap_inboxes
from mimir.web import bp_web


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = settings.secret_key
    app.config["DEBUG"] = settings.flask_debug
    app.register_blueprint(bp_web)
    register_cli(app)
    with SessionLocal() as session:
        bootstrap_inboxes(session)
    return app
