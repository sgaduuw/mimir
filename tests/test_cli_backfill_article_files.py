"""CLI + service test for `backfill-article-files`. The extractor
itself is exercised in `tests/test_patches.py`; ingest-time path
extraction is exercised in `tests/test_ingest.py`. This file pins
the backfill walker's idempotence + the bucket counters."""
from pathlib import Path

from click.testing import CliRunner
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo
from sqlalchemy import select

from mimir.cli import backfill_article_files_command
from mimir.ingest import ingest_epoch
from mimir.models import Article, ArticleFile, Inbox
from mimir.patches import backfill_article_files


def _build_pubinbox_repo(repo_path: Path, messages: list[bytes]) -> Path:
    """Mirror of the helper in tests/test_ingest.py; duplicating
    avoids importing from a sibling test module (pytest test files
    aren't meant to be importable cross-module)."""
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


_PATCH_BODY = (
    b"Signed-off-by: A <a@example>\n\n"
    b"diff --git a/fs/foo/a.c b/fs/foo/a.c\n"
    b"@@ -1 +1 @@\n-x\n+y\n"
)
_PROSE_BODY = b"hello, no diff here\n"


def _rfc5322(msgid: str, body: bytes) -> bytes:
    return (
        b"Message-ID: <" + msgid.encode() + b">\r\n"
        b"From: a@b.example\r\n"
        b"Subject: t\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        b"\r\n" + body
    )


def _ingest_articles_without_files(seeded_db, tmp_path, *bodies):
    """Ingest articles via the real pipeline, then DELETE the
    ArticleFile rows so the backfill has something to do. This
    simulates the pre-extractor world (articles ingested before
    the feature landed)."""
    alpha = None
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.expunge(alpha)
    epoch_path = tmp_path / "0.git"
    _build_pubinbox_repo(epoch_path, [
        _rfc5322(f"m{i}@example.com", body) for i, body in enumerate(bodies)
    ])
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        alpha.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, alpha, "0.git", epoch_path, workers=1)
        # Drop the rows ingest just created so backfill has work.
        s.query(ArticleFile).delete()
        s.commit()


def test_backfill_indexes_patch_articles(seeded_db, tmp_path):
    """Pre-extractor articles get re-walked: patch bodies land
    ArticleFile rows; prose bodies don't.

    `--limit=2` scopes to just the freshly-ingested articles
    (newest-first walk; the seeded conftest articles have lower
    IDs and bogus mirror SHAs so they'd otherwise dominate the
    `skipped` bucket and dilute the signal we care about)."""
    _ingest_articles_without_files(
        seeded_db, tmp_path, _PATCH_BODY, _PROSE_BODY,
    )

    result = backfill_article_files(limit=2)
    assert result.examined == 2
    assert result.indexed == 1
    assert result.no_diff == 1
    assert result.failed == 0

    with seeded_db() as s:
        rows = {
            (a.message_id, f.path) for a, f in s.execute(
                select(Article, ArticleFile)
                .join(ArticleFile, ArticleFile.article_id == Article.id)
            ).all()
        }
    assert rows == {("m0@example.com", "fs/foo/a.c")}


def test_backfill_is_idempotent_on_rerun(seeded_db, tmp_path):
    """Second run with no new articles → the freshly-indexed one
    lands in `skipped` (rows already exist). No duplicate rows."""
    _ingest_articles_without_files(seeded_db, tmp_path, _PATCH_BODY)
    backfill_article_files(limit=1)
    second = backfill_article_files(limit=1)
    assert second.examined == 1
    assert second.skipped == 1
    assert second.indexed == 0

    with seeded_db() as s:
        paths = [r.path for r in s.execute(select(ArticleFile)).scalars()]
    # Exactly one row, re-run didn't duplicate.
    assert paths == ["fs/foo/a.c"]


def test_backfill_reprocess_re_extracts(seeded_db, tmp_path):
    """`--reprocess` deletes existing rows and re-extracts. Useful
    after an extractor change ships."""
    _ingest_articles_without_files(seeded_db, tmp_path, _PATCH_BODY)
    backfill_article_files(limit=1)
    result = backfill_article_files(limit=1, reprocess=True)
    assert result.examined == 1
    assert result.indexed == 1
    assert result.skipped == 0


def test_backfill_cli_prints_summary(seeded_db, tmp_path):
    """End-to-end CLI: runs without error, summary line in output."""
    _ingest_articles_without_files(
        seeded_db, tmp_path, _PATCH_BODY, _PROSE_BODY,
    )
    result = CliRunner().invoke(
        backfill_article_files_command, ["--limit", "2"],
    )
    assert result.exit_code == 0, result.output
    assert "examined=2" in result.output
    assert "indexed=1" in result.output
    assert "no_diff=1" in result.output


def test_backfill_cli_honours_limit(seeded_db, tmp_path):
    """`--limit` caps the per-session examination so a huge archive
    can be backfilled in chunks."""
    _ingest_articles_without_files(
        seeded_db, tmp_path, _PATCH_BODY, _PATCH_BODY, _PATCH_BODY,
    )
    result = CliRunner().invoke(backfill_article_files_command, ["--limit", "2"])
    assert result.exit_code == 0
    assert "examined=2" in result.output


def test_backfill_skips_articles_with_unreachable_mirror(seeded_db):
    """Seeded conftest articles have bogus SHAs in their
    ArticleList rows (the fixture never reads their bodies, so it
    didn't need real ones). The backfill must treat those as
    `skipped`, not `failed`, they're a mirror-state condition,
    not an extractor regression. Pins the dulwich-KeyError →
    skipped path."""
    result = backfill_article_files()
    assert result.failed == 0
    assert result.skipped > 0
