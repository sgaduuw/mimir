from datetime import UTC, datetime
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from jinja2 import ChoiceLoader, DictLoader


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


@pytest.mark.parametrize("template", ["author.html", "reviewer.html"])
def test_message_listing_keeps_calendar_date_visible(template):
    from mimir import create_app

    app = create_app()
    env = app.jinja_env.overlay(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                app.jinja_loader,
            ]
        )
    )
    date = datetime(2024, 3, 10, 12, 34, tzinfo=UTC)
    row = SimpleNamespace(
        id=12,
        article_id=12,
        subject="Example patch",
        author=None,
        date=date,
        inbox_name="alpha",
        role="Reviewed-by",
    )
    with app.test_request_context("/"):
        html = env.get_template(template).render(
            inbox_name="alpha",
            sub="Example",
            address="reviewer@example.net",
            results=[row],
            entries=[row],
            role_counts={"Reviewed-by": 1},
            total=1,
            truncated=False,
            lifecycle_status_by_id={},
        )
    visible = VisibleText()
    visible.feed(html)
    assert "2024-03-10" in "".join(visible.parts)
