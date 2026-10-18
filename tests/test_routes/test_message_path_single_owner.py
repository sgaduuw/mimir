"""`mimir.web.urls._msg_path` is the one place a message URL is spelled
(#768). Two Python call sites and six template links built it by hand.
For an undated row the templates raised (`'None' has no attribute
'month'`) and the related-threads link read `/<inbox>/0/00/<id>`, where
the helper gives `/<inbox>/0000/00/<id>`. Ingest always dates articles,
so this was latent rather than seen, which is why a sweep guards it."""

import re
from pathlib import Path

from mimir.web.urls import _msg_path

ROOT = Path(__file__).resolve().parents[2] / "mimir"

# A hand-built message path: a year followed by a zero-padded month, in a
# Python f-string or a Jinja expression.
# Also `%Y/%m` or a `02`-padded month with an id later on the line, so a
# new spelling is caught too; month-archive links carry no id.
_PY_HAND_BUILT = re.compile(r"\.year\}/\{[^}]*\.month:02d\}|(%Y/%m|month:02d?\}).*id\}")
# Year, month and an id: a message, not a month archive like daily.html's.
_JINJA_HAND_BUILT = re.compile(
    r"(\.year\s*\}\}/.*month|%Y/%m|%02d).*\}\}/\{\{[^}]*id\s*\}\}"
)


def test_no_message_path_is_built_by_hand():
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in sorted(ROOT.rglob("*.py"))
        if path.name != "urls.py"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if _PY_HAND_BUILT.search(line)
    ] + [
        f"{path.relative_to(ROOT)}:{n}"
        for path in sorted((ROOT / "templates").rglob("*.html"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if _JINJA_HAND_BUILT.search(line)
    ]
    assert offenders == []


def test_templates_spell_message_paths_through_the_helper(client):
    """The `msg_path` template global is `_msg_path`, so an undated row
    links to `/<inbox>/0000/00/<id>` like every other surface."""
    from datetime import UTC, datetime

    from flask import render_template_string

    with client.application.test_request_context():
        dated = datetime(2024, 3, 9, tzinfo=UTC)
        for date in (dated, None):
            rendered = render_template_string(
                "{{ msg_path(article_id, date, inbox_name) }}",
                article_id=42,
                date=date,
                inbox_name="lkml",
            )
            assert rendered == _msg_path(42, date, "lkml")
    assert _msg_path(42, None, "lkml") == "/lkml/0000/00/42"


def test_related_thread_url_uses_the_helper_for_undated_roots():
    """An undated root is stored as year 0; the template once spelled that
    `/alpha/0/00/7`, while the route serves `_msg_path`'s `/alpha/0000/00/7`."""
    from datetime import UTC, datetime

    from mimir.related import RelatedThread

    def thread(year, month):
        return RelatedThread(
            article_id=7,
            inbox_name="alpha",
            year=year,
            month=month,
            subject="s",
            last_activity=None,
            score=1.0,
            signals=("subject",),
        )

    assert thread(0, 0).url == "/alpha/0000/00/7"
    assert thread(2026, 3).url == _msg_path(
        7, datetime(2026, 3, 9, tzinfo=UTC), "alpha"
    )
