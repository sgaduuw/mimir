"""Shared helpers for tests/test_cli/*.py.

Hoisted from the pre-split tests/test_cli.py so per-bucket
test modules can import what they need. Underscore-prefixed
filename so pytest does not collect this as a test module.
"""
from datetime import datetime, timezone
from pathlib import Path

from click.testing import CliRunner
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo
from sqlalchemy import select

from mimir.cli import ingest_command
from mimir.models import (
    Inbox, ParseFailure,
)


def _rfc5322_msg(msgid: str, *, subject: str = "t", body: bytes = b"hello") -> bytes:
    return (
        b"Message-ID: <" + msgid.encode() + b">\r\n"
        b"From: a@example.com\r\n"
        b"Subject: " + subject.encode() + b"\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        b"\r\n"
        + body
    )


def _build_pubinbox_repo(repo_path: Path, messages: list[bytes]) -> Path:
    """Build a bare public-inbox v2 epoch repo: one commit per
    message, each tree carries an `m` blob with raw RFC 5322 bytes."""
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    repo = Repo.init_bare(str(repo_path), mkdir=True)
    parent = None
    for i, raw in enumerate(messages):
        blob = Blob.from_string(raw)
        repo.object_store.add_object(blob)
        tree = Tree()
        tree.add(b"m", 0o100644, blob.id)
        repo.object_store.add_object(tree)
        c = Commit()
        c.tree = tree.id
        c.parents = [parent] if parent else []
        c.author = c.committer = b"test <t@x>"
        c.commit_time = c.author_time = 1704067200 + i
        c.commit_timezone = c.author_timezone = 0
        c.encoding = b"UTF-8"
        c.message = f"add msg {i}".encode()
        repo.object_store.add_object(c)
        parent = c.id
    if parent is not None:
        repo.refs[b"HEAD"] = parent
    return repo_path


def _repoint_inbox(name: str, mirror_dir: Path) -> None:
    """Repoint a seeded Inbox row's mirror_path at the given tmp dir
    so the read path resolves blobs against a real repo."""
    from mimir.extensions import SessionLocal
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == name)).scalar_one()
        ix.mirror_path = str(mirror_dir)
        s.commit()


# `ingest` -- batch ingest CLI.


def _ingest_one_for_show(tmp_path) -> tuple[str, Path]:
    """Build a single-message epoch and ingest it, returning the
    message-id and the mirror dir. Tests then drive `show` against
    the seeded article. Uses epoch 2.git to avoid colliding with
    seeded fixture rows at 0.git."""
    msgid = "show-msg@example.com"
    mirror = tmp_path / "alpha-mirror"
    _build_pubinbox_repo(mirror / "2.git", [
        _rfc5322_msg(msgid, subject="show test subject", body=b"hello show body"),
    ])
    _repoint_inbox("alpha", mirror)
    CliRunner().invoke(ingest_command, ["--inbox", "alpha"])
    return msgid, mirror


def _seed_parse_failure(inbox_name: str = "alpha") -> None:
    """Insert one ParseFailure row tied to the seeded inbox."""
    from sqlalchemy import select
    from mimir.extensions import SessionLocal
    with SessionLocal() as s:
        ix = s.execute(
            select(Inbox).where(Inbox.name == inbox_name)
        ).scalar_one()
        now = datetime.now(timezone.utc)
        s.add(ParseFailure(
            inbox_id=ix.id,
            epoch="0.git",
            commit_sha="ab" * 20,
            error_class="MessageTooLarge",
            error_message="message exceeds cap",
            first_seen=now,
            last_attempt=now,
            attempts=1,
        ))
        s.commit()
