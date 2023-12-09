from flask import Flask

from mimir.config import Config

from mimir.extensions import db
from mimir.routes.main import bp_main

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    register_extensions(app)
    register_blueprints(app)
    return app

def register_extensions(app):
    """Register Flask extensions."""
    db.init_app(app)
    return None

def register_blueprints(app):
    """Register Flask blueprints."""
    app.register_blueprint(bp_main)
    return None

if __name__ == 'main':
    app = create_app()
    app.run()
