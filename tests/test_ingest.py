"""Outcome-bucket contract for `mimir.ingest`.

Every commit walked must land in exactly one of:
    new / linked / dup_batch / dup_db / failed

This file pins each bucket via a tiny ephemeral public-inbox-shaped
git repo built with dulwich, then drives `ingest_epoch` against it
with workers=1 (deterministic, stays in-process).
"""
from pathlib import Path

from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo
from sqlalchemy import select

from mimir.ingest import ingest_epoch
from mimir.models import Article, ArticleList, Inbox, IngestState


def _rfc5322(msgid: str, body: bytes = b"hello") -> bytes:
    """Minimal valid RFC 5322 message."""
    return (
        b"Message-ID: <" + msgid.encode() + b">\r\n"
        b"From: a@b.example\r\n"
        b"Subject: t\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        b"\r\n"
        + body
    )


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


def test_ingest_new_message_creates_article(seeded_db, tmp_path):
    alpha = _alpha(seeded_db)
    _build_pubinbox_repo(tmp_path / "0.git", [_rfc5322("fresh@example.com")])

    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.new == 1
    assert result.linked == 0
    assert result.dup_batch == 0
    assert result.dup_db == 0
    assert result.failed == 0

    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "fresh@example.com")
        ).scalar_one()
        link = s.execute(
            select(ArticleList).where(
                ArticleList.article_id == art.id,
                ArticleList.inbox_id == alpha.id,
            )
        ).scalar_one()
        assert link.epoch == "0.git"


# Bucket: linked (cross-post)


def test_ingest_linked_when_message_id_already_in_other_inbox(seeded_db, tmp_path):
    """art2@example.com is in beta (seeded). Ingesting it into alpha
    must reuse the existing Article and add an article_lists row —
    that's the `linked` bucket."""
    alpha = _alpha(seeded_db)
    _build_pubinbox_repo(tmp_path / "0.git", [_rfc5322("art2@example.com")])

    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.linked == 1
    assert result.new == 0
    assert result.dup_batch == 0
    assert result.dup_db == 0

    with seeded_db() as s:
        # Same Article, now linked to both inboxes.
        art = s.execute(
            select(Article).where(Article.message_id == "art2@example.com")
        ).scalar_one()
        inbox_ids = {
            row.inbox_id for row in s.execute(
                select(ArticleList).where(ArticleList.article_id == art.id)
            ).scalars()
        }
        assert len(inbox_ids) == 2  # beta (seed) + alpha (this ingest)


# Bucket: dup_batch


def test_ingest_dup_batch_skips_in_same_walk(seeded_db, tmp_path):
    """Two commits in the same batch carrying the same Message-ID:
    the second lands in dup_batch."""
    alpha = _alpha(seeded_db)
    _build_pubinbox_repo(tmp_path / "0.git", [
        _rfc5322("twin@example.com", body=b"first"),
        _rfc5322("twin@example.com", body=b"second"),
    ])

    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.new == 1
    assert result.dup_batch == 1
    assert result.linked == 0
    assert result.dup_db == 0
    assert result.failed == 0


# Bucket: dup_db (re-walk)


def test_ingest_dup_db_when_rewalking_existing_inbox(seeded_db, tmp_path):
    """Run ingest_epoch twice against the same repo. After the
    first run, the article is in DB and linked to alpha; the second
    run, with rewound IngestState, sees it as `dup_db`."""
    alpha = _alpha(seeded_db)
    _build_pubinbox_repo(tmp_path / "0.git", [_rfc5322("once@example.com")])

    with seeded_db() as s:
        first = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)
    assert first.new == 1

    # Rewind state so the walker re-emits the same commit.
    with seeded_db() as s:
        state = s.execute(
            select(IngestState).where(
                IngestState.inbox_id == alpha.id,
                IngestState.epoch == "0.git",
            )
        ).scalar_one()
        state.last_commit_sha = None
        s.commit()

    with seeded_db() as s:
        second = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert second.dup_db == 1
    assert second.new == 0
    assert second.linked == 0
    assert second.dup_batch == 0


# Bucket: failed


def test_ingest_failed_on_unparseable_message(seeded_db, tmp_path):
    """A message with no Message-ID raises ValueError inside
    parse_message, gets caught by the worker, counted as `failed`,
    and the walker advances past it."""
    alpha = _alpha(seeded_db)
    bad = b"From: a@b.example\r\nSubject: no msgid\r\n\r\nbody"
    _build_pubinbox_repo(tmp_path / "0.git", [
        _rfc5322("good@example.com"),
        bad,
    ])

    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.new == 1
    assert result.failed == 1
    assert result.dup_batch == 0
    assert result.dup_db == 0
    assert result.linked == 0


def test_ingest_failed_on_oversized_message(seeded_db, tmp_path, monkeypatch):
    """A message above the size cap raises MessageTooLarge in
    parse_message; counted as failed."""
    import mimir.parser

    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 200)

    alpha = _alpha(seeded_db)
    huge_body = b"x" * 500
    _build_pubinbox_repo(tmp_path / "0.git", [
        _rfc5322("over@example.com", body=huge_body),
    ])

    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.failed == 1
    assert result.new == 0


# IngestState resume


def test_ingest_state_advances_on_each_batch(seeded_db, tmp_path):
    """After ingest, IngestState.last_commit_sha equals the HEAD of
    the repo we just walked."""
    alpha = _alpha(seeded_db)
    repo_path = tmp_path / "0.git"
    _build_pubinbox_repo(repo_path, [_rfc5322(f"m{i}@example.com") for i in range(3)])

    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", repo_path, workers=1)

    head = Repo(str(repo_path)).head().decode()
    with seeded_db() as s:
        state = s.execute(
            select(IngestState).where(
                IngestState.inbox_id == alpha.id,
                IngestState.epoch == "0.git",
            )
        ).scalar_one()
    assert state.last_commit_sha == head


def test_ingest_resume_skips_already_walked_commits(seeded_db, tmp_path):
    """A second ingest_epoch without rewinding state walks zero
    commits: the dulwich excludes-set covers HEAD."""
    alpha = _alpha(seeded_db)
    repo_path = tmp_path / "0.git"
    _build_pubinbox_repo(repo_path, [_rfc5322(f"r{i}@example.com") for i in range(2)])

    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", repo_path, workers=1)
        second = ingest_epoch(s, alpha, "0.git", repo_path, workers=1)

    assert second.new == 0
    assert second.linked == 0
    assert second.dup_batch == 0
    assert second.dup_db == 0
    assert second.failed == 0
