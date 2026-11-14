"""Authoritative L: contacts remain separate from personal allowlisting."""

import pytest
from dulwich.objects import Blob
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from mimir.mainline import load_maintainers
from mimir.models import MainlineState, Subsystem
from tests.test_cli_maintainers import _build_mainline_repo

SOURCE = b"""FIRST
M: Person <person@example.org>
L:  LIST@Example.ORG (moderated)
L: list@example.org
L: Named list <second@custom.example>
L: not-an-address
L: invalid <>
L: bad@@example.org
L: bad <broken
L: li\x00st@example.org
L: sec\xffond@custom.example
F: first/

SECOND
L: list@example.org
F: second/
"""


def _mapping(session):
    return session.execute(
        text(
            "SELECT s.name, l.address FROM subsystem_lists l "
            "JOIN subsystems s ON s.id=l.subsystem_id ORDER BY s.name, l.address"
        )
    ).all()


def test_contacts_are_normalized_deduplicated_and_not_personal_allowlist(
    seeded_db, tmp_path, monkeypatch
):
    from mimir import maintainer_allowlist
    from mimir.config import settings

    monkeypatch.setattr(settings, "list_host_suffix_overrides", [])
    path = tmp_path / "linux.git"
    _build_mainline_repo(path, SOURCE)
    assert load_maintainers(path)[:2] == (True, 2)
    with seeded_db() as session:
        assert _mapping(session) == [
            ("FIRST", "list@example.org"),
            ("FIRST", "second@custom.example"),
            ("SECOND", "list@example.org"),
        ]
    # Only M:/R: entries can join the display allowlist.
    assert "person@example.org" in maintainer_allowlist.maintainer_addresses()
    assert "list@example.org" not in maintainer_allowlist.maintainer_addresses()


def test_unchanged_blob_requires_force_to_populate_and_each_force_moves_generation(
    seeded_db, tmp_path
):
    path = tmp_path / "linux.git"
    _build_mainline_repo(path, SOURCE)
    blob_sha = Blob.from_string(SOURCE).id.decode()
    with seeded_db() as session:
        session.add(Subsystem(name="OLD", status="Maintained"))
        session.add(MainlineState(tree_name="linus", last_commit_sha=blob_sha))
        session.commit()
    assert load_maintainers(path)[0] is False
    with seeded_db() as session:
        assert _mapping(session) == []
    for generation in (1, 2):
        assert load_maintainers(path, force=True)[0] is True
        with seeded_db() as session:
            assert len(_mapping(session)) == 3
            assert (
                session.get(MainlineState, "linus").last_commit_sha
                == f"{blob_sha}.{generation}"
            )
        assert load_maintainers(path)[0] is False


def test_replace_removes_old_contacts_and_a_failed_replace_rolls_back_every_table(
    seeded_db, tmp_path
):
    from mimir.broker import _context
    from mimir.broker.writes import WriteOp

    path = tmp_path / "linux.git"
    head = _build_mainline_repo(path, SOURCE)
    load_maintainers(path)
    with seeded_db() as session:
        before = _mapping(session)
        cursor = session.get(MainlineState, "linus").last_commit_sha
        ids = session.scalars(select(Subsystem.id).order_by(Subsystem.id)).all()
    replacement = (
        b"REPLACEMENT\nM: Other <other@example.org>\nL: fresh@custom.example\nF: new/\n"
    )
    _build_mainline_repo(path, replacement, parent=head)
    writer = _context.get_active_writer()
    # Fail at the final cursor update, after every replacement table was
    # written. A separate transaction would expose a partial replacement.
    writer.submit(
        WriteOp(
            label="test:list-rollback",
            fn=lambda c: c.execute(
                text(
                    "CREATE TRIGGER reject_rules_cursor BEFORE UPDATE ON mainline_state "
                    "BEGIN SELECT RAISE(ABORT, 'injected rules failure'); END"
                )
            ),
        )
    ).result()
    try:
        with pytest.raises(IntegrityError, match="injected rules failure"):
            load_maintainers(path)
        with seeded_db() as session:
            assert _mapping(session) == before
            assert session.get(MainlineState, "linus").last_commit_sha == cursor
            assert (
                session.scalars(select(Subsystem.id).order_by(Subsystem.id)).all()
                == ids
            )
            assert session.execute(
                text("SELECT glob FROM subsystem_paths ORDER BY glob")
            ).scalars().all() == ["first/", "second/"]
            assert session.execute(
                text("SELECT address FROM subsystem_maintainers")
            ).scalars().all() == ["person@example.org"]
    finally:
        writer.submit(
            WriteOp(
                label="test:list-rollback-cleanup",
                fn=lambda c: c.execute(text("DROP TRIGGER reject_rules_cursor")),
            )
        ).result()
    assert load_maintainers(path)[0] is True
    with seeded_db() as session:
        assert _mapping(session) == [("REPLACEMENT", "fresh@custom.example")]
        assert session.get(MainlineState, "linus").last_commit_sha != cursor


def test_population_cli_uses_a_separate_broker_and_bypasses_cadence_without_walking(
    tmp_path,
):
    """No test-process active writer can supply missing broker wiring."""
    import json
    import os
    import sqlite3
    import subprocess
    import sys
    import tempfile
    import time
    from pathlib import Path

    from tests.test_migrations import REPO, _alembic

    db = tmp_path / "broker.db"
    result = _alembic(db, "head")
    assert result.returncode == 0, result.stderr
    tree = tmp_path / "linux.git"
    _build_mainline_repo(tree, SOURCE)
    blob_sha = Blob.from_string(SOURCE).id.decode()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO mainline_state(tree_name,last_commit_sha,last_walked_at,commits_walked_to_sha) "
            "VALUES('linus',?,datetime('now'),'do-not-walk')",
            (blob_sha,),
        )
    with tempfile.TemporaryDirectory(prefix="mimir-lists-", dir="/tmp") as scratch:
        socket = Path(scratch) / "b.sock"
        env = {
            **{
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("TREES", "INBOXES"))
            },
            "DATABASE_URL": f"sqlite:///{db}",
            "MIMIR_ENV_FILE": "",
            "BROKER_SOCKET_PATH": str(socket),
            "INBOXES": "{}",
            "SECRET_KEY": "subsystem-list-test-not-a-production-secret",
            "TREES": json.dumps(
                {
                    "linus": {
                        "url": "https://example.org/linux.git",
                        "path": str(tree),
                        "walk_every_seconds": 3600,
                    }
                }
            ),
        }
        command = [sys.executable, "-c", "from mimir.cli import mimir; mimir()"]
        with (tmp_path / "broker.log").open("w+") as log:
            proc = subprocess.Popen(
                command + ["broker", "--socket", str(socket)],
                cwd=REPO,
                env={**env, "MIMIR_IS_BROKER": "true"},
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 10
                while (
                    not socket.exists()
                    and proc.poll() is None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                log.flush()
                assert socket.exists(), (tmp_path / "broker.log").read_text()

                def run(*flags):
                    result = subprocess.run(
                        command
                        + ["update-mainline", "--skip-fetch", "--skip-commits", *flags],
                        cwd=REPO,
                        env={**env, "MIMIR_IS_BROKER": "false"},
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    assert result.returncode == 0, result.stdout + result.stderr
                    return result

                # Ordinary runs respect cadence; force alone bypasses it.
                outcome = run()
                assert outcome.stdout.startswith("update-mainline: linus skipped (")
                with sqlite3.connect(db) as conn:
                    assert (
                        conn.execute("SELECT count(*) FROM subsystem_lists").fetchone()[
                            0
                        ]
                        == 0
                    )
                for generation in (1, 2):
                    outcome = run("--force")
                    assert "loaded 2 subsystems" in outcome.stdout
                    with sqlite3.connect(db) as conn:
                        assert (
                            conn.execute(
                                "SELECT count(*) FROM subsystem_lists"
                            ).fetchone()[0]
                            == 3
                        )
                        assert conn.execute(
                            "SELECT last_commit_sha,commits_walked_to_sha FROM mainline_state WHERE tree_name='linus'"
                        ).fetchone() == (f"{blob_sha}.{generation}", "do-not-walk")
                        assert (
                            conn.execute(
                                "SELECT count(*) FROM mainline_commits"
                            ).fetchone()[0]
                            == 0
                        )
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
