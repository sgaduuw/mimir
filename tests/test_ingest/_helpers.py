"""Shared helpers for tests/test_ingest/*.py.

Hoisted from the pre-split tests/test_ingest.py so per-bucket
test modules can import what they need. Underscore-prefixed
filename so pytest does not collect this as a test module.
"""

from pathlib import Path

from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo
from sqlalchemy import delete, select

from mimir.ingest import ingest_epoch
from mimir.models import (
    Article,
    ArticleList,
    Inbox,
    InboxAddressObservation,
)


def _rfc5322(
    msgid: str,
    body: bytes = b"hello",
    to: str | None = None,
    cc: str | None = None,
    in_reply_to: str | None = None,
) -> bytes:
    """Minimal valid RFC 5322 message. Optional To/Cc let canonical-
    inbox tests inject list addresses; `in_reply_to` builds a real
    multi-message thread, which is what any test about thread roots
    needs (a single-message thread roots at itself no matter what the
    code under test does, so it cannot distinguish a working
    implementation from a broken one)."""
    parts = [
        b"Message-ID: <" + msgid.encode() + b">\r\n",
        b"From: a@b.example\r\n",
    ]
    if to is not None:
        parts.append(b"To: " + to.encode() + b"\r\n")
    if cc is not None:
        parts.append(b"Cc: " + cc.encode() + b"\r\n")
    if in_reply_to is not None:
        parts.append(b"In-Reply-To: <" + in_reply_to.encode() + b">\r\n")
    parts.extend(
        [
            b"Subject: t\r\n",
            b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n",
            b"\r\n",
            body,
        ]
    )
    return b"".join(parts)


def _build_pubinbox_repo(repo_path: Path, messages: list[bytes]) -> Path:
    """Build a bare public-inbox v2-shaped repo: one commit per
    message, the tree carries an `m` blob with the raw RFC 5322
    bytes. Returns the repo path."""
    repo = Repo.init_bare(str(repo_path), mkdir=True)
    parent = None
    for i, raw in enumerate(messages):
        blob = Blob.from_string(raw)
        repo.object_store.add_object(blob)

        tree = Tree()
        tree.add(b"m", 0o100644, blob.id)
        repo.object_store.add_object(tree)

        commit = Commit()
        commit.tree = tree.id
        commit.parents = [parent] if parent else []
        commit.author = commit.committer = b"test <t@x>"
        commit.commit_time = commit.author_time = 1700000000 + i
        commit.commit_timezone = commit.author_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = f"add message {i}".encode()
        repo.object_store.add_object(commit)
        parent = commit.id

    if parent is not None:
        repo.refs[b"HEAD"] = parent
    return repo_path


def _alpha(seeded_db) -> Inbox:
    with seeded_db() as s:
        return s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()


# Bucket: new


def _setup_alpha_with_messages(seeded_db, tmp_path, n: int) -> Inbox:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _build_pubinbox_repo(
        mirror / "0.git",
        [_rfc5322(f"auto{i}@example.com") for i in range(n)],
    )
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.mirror_path = str(mirror)
        s.commit()
        return s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()


def _spy_text(monkeypatch) -> list[str]:
    from mimir.ingest import orchestrate as ingest_mod

    seen: list[str] = []
    real_text = ingest_mod.text

    def _spy(stmt):
        seen.append(stmt)
        return real_text(stmt)

    monkeypatch.setattr(ingest_mod, "text", _spy)
    return seen


def _clear_seed_articles(seeded_db) -> None:
    """Drop the conftest-seeded articles + observations so backfill
    tests only see what THIS test inserts. Inboxes (alpha, beta) stay
    so we still have IDs and a place to set list_address."""
    with seeded_db() as s:
        s.execute(delete(InboxAddressObservation))
        s.execute(delete(ArticleList))
        s.execute(delete(Article))
        s.commit()


def _ingest_with_to(
    seeded_db,
    tmp_path,
    inbox: Inbox,
    msgid: str,
    to: str | None = None,
    cc: str | None = None,
    repo_dir: str = "0.git",
) -> None:
    """Ingest a single message with optional To/Cc into `inbox`."""
    raw = _rfc5322(msgid, to=to, cc=cc)
    repo_path = tmp_path / repo_dir
    if not repo_path.exists():
        _build_pubinbox_repo(repo_path, [raw])
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == inbox.id)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, repo_dir, repo_path, workers=1)


def _rfc5322_with_date(msgid: str, date_header: str, to: str) -> bytes:
    return (
        b"Message-ID: <" + msgid.encode() + b">\r\n"
        b"From: a@b.example\r\n"
        b"To: " + to.encode() + b"\r\n"
        b"Subject: t\r\n"
        b"Date: " + date_header.encode() + b"\r\n"
        b"\r\n"
        b"hi"
    )


def _naive_utc(dt):
    """SQLite stores DateTime as TEXT without tz; round-trip strips
    tzinfo. Compare on the naive form for in-DB assertions."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _cache_set_direct(key: str, value, ttl: int) -> None:
    """Write a cache value synchronously, bypassing the active
    WriterThread's async routing.

    When `broker_active` is in effect, `cache.set()` routes writes
    through `set_via_writer()` (fire-and-forget). Tests that need a
    sentinel value to be readable *immediately* after the write (e.g.
    to prove the post-ingest warm does not overwrite a fresh row) use
    this helper instead of `cache.set()`.

    Calls `cache._direct_set` with the pre-namespaced key and
    pre-encoded payload, which opens its own SessionLocal and commits
    synchronously."""
    import json

    from mimir import cache

    nskey = cache._ns(key)
    payload = json.dumps(cache._encode(value), separators=(",", ":"))
    cache._direct_set(nskey, payload, ttl)


def _drain_writer() -> None:
    """Block until the active WriterThread has committed every op
    submitted before this call.

    The WriterThread processes ops sequentially. Submitting a no-op
    WriteOp and blocking on its Future guarantees that all previously
    queued ops (including fire-and-forget `set_via_writer` calls from
    `cache.set`) have committed to SQLite by the time this returns.

    Use after `ingest_inbox()` / `ingest_epoch()` in tests that
    assert the post-ingest cache state (post-warm writes are
    fire-and-forget and may not have committed yet when the ingest
    call returns)."""
    from mimir.broker import _context
    from mimir.broker.writes import WriteOp

    writer = _context.get_active_writer()
    writer.submit(WriteOp(label="test:drain", fn=lambda conn: None)).result(timeout=10)
