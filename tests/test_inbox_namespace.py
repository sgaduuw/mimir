"""Root routes and inbox slugs must never claim the same namespace."""

import pytest

from mimir._inbox_name import INBOX_NAME_RE


@pytest.mark.parametrize(
    "name",
    [
        "maintainers",
        "subsystems",
        "merge-window",
        "privacy",
        "static",
        "api",
        "m",
        "healthz",
        "readyz",
    ],
)
def test_root_route_words_cannot_be_inbox_names(name):
    assert INBOX_NAME_RE.fullmatch(name) is None


def test_every_literal_root_segment_is_reserved(client):
    from mimir._inbox_name import RESERVED_ROOT_WORDS

    first_segments = {
        first
        for rule in client.application.url_map.iter_rules()
        if (first := rule.rule.lstrip("/").split("/")[0]) and "<" not in first
    }
    assert first_segments <= RESERVED_ROOT_WORDS
    assert all(INBOX_NAME_RE.fullmatch(name) is None for name in first_segments)


def test_old_image_inventory_reports_configured_and_stored_conflicts(tmp_path):
    import json
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    configured = tmp_path / "old-configured.json"
    configured.write_text(json.dumps(["alpha", "maintainers"]))
    database = tmp_path / "old.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE inboxes(name TEXT)")
        conn.executemany("INSERT INTO inboxes VALUES(?)", [("beta",), ("privacy",)])
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "tools" / "check_inbox_namespace.py"),
            "--configured",
            str(configured),
            "--database",
            str(database),
        ],
        env={**os.environ, "SECRET_KEY": "", "INBOXES": '{"maintainers": {}}'},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout) == {
        "configured_count": 2,
        "stored_count": 2,
        "configured_conflicts": ["maintainers"],
        "stored_conflicts": ["privacy"],
    }
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT name FROM inboxes ORDER BY name").fetchall() == [
            ("beta",),
            ("privacy",),
        ]


def test_startup_rejects_stored_reserved_names_despite_bootstrap_sentinel(
    seeded_db, monkeypatch, tmp_path
):
    from mimir.broker import startup
    from mimir.inboxes import InboxValidationError
    from mimir.models import Inbox

    with seeded_db() as session:
        session.add(
            Inbox(
                name="privacy",
                mirror_path="/old/privacy",
                upstream_url="https://example.com/privacy",
            )
        )
        session.commit()
    sentinel = tmp_path / ".bootstrapped"
    sentinel.touch()
    monkeypatch.setattr(startup, "_migrate_if_needed", lambda path: False)
    monkeypatch.setattr(
        startup,
        "_bootstrap_inboxes_if_needed",
        lambda path: pytest.fail("namespace guard did not stop bootstrap"),
    )
    with pytest.raises(InboxValidationError, match="privacy"):
        startup.prepare_database(tmp_path / "broker.sock")
    assert sentinel.exists()
    with seeded_db() as session:
        assert session.query(Inbox).filter_by(name="privacy").one().name == "privacy"
