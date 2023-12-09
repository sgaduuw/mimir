from flask import Blueprint

bp_main = Blueprint('main', __name__)

from mimir.routes.main import load_data
