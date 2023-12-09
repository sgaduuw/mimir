from mimir.routes.main import bp_main
from mimir.models import Article

@bp_main.route('/import/')
def import_page() -> str:
    pass