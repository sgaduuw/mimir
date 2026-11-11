"""The web factory makes every app log record one JSON line."""

import json
import logging

from mimir import create_web_app


def test_web_log_records_are_json_lines(monkeypatch, capsys):
    # pytest gives root its capture handlers; the factory installs its
    # handler only on a bare root, as in gunicorn.
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    create_web_app()

    logging.getLogger("mimir.web.crawler_budget").warning(
        "crawler budget: crawler=%s deferred=%d", "Applebot", 3
    )
    try:
        1 / 0
    except ZeroDivisionError:
        logging.getLogger("mimir").exception("boom")

    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    budget, boom = map(json.loads, lines)
    assert budget["level"] == "WARNING"
    assert budget["logger"] == "mimir.web.crawler_budget"
    assert budget["msg"] == "crawler budget: crawler=Applebot deferred=3"
    assert budget["ts"].endswith("+00:00")
    assert "exc" not in budget
    assert boom["msg"] == "boom"
    assert "ZeroDivisionError" in boom["exc"]
