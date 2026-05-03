from flask import Flask

from mimir.cli import register_cli
from mimir.config import settings
from mimir.web import bp_web


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = settings.secret_key
    app.config["DEBUG"] = settings.flask_debug
    app.register_blueprint(bp_web)
    register_cli(app)
    return app


app = create_app()
