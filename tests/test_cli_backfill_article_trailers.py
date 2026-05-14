"""CLI + service test for `backfill-article-trailers`. The
extractor itself is exercised in `tests/test_trailers.py`; ingest-
time trailer extraction is covered by `tests/test_ingest.py`. This
file pins the backfill walker's idempotence + bucket counters."""
from pathlib import Path

from click.testing import CliRunner
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo
from sqlalchemy import select

from mimir.cli import backfill_article_trailers_command
from mimir.ingest import ingest_epoch
from mimir.models import Article, ArticleTrailer, Inbox
from mimir.trailers import backfill_article_trailers


def _build_pubinbox_repo(repo_path: Path, messages: list[bytes]) -> Path:
    """Mirror of the helper in tests/test_cli_backfill_article_files.py;
    duplicating to avoid cross-test-module imports."""
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


_TRAILER_BODY = (
    b"Looks good.\n\n"
    b"Reviewed-by: Alice <Alice@Example.COM>\n"
    b"Acked-by: Bob <bob@kernel.org>\n"
)
_PROSE_BODY = b"hello, no trailers here\n"


def _rfc5322(msgid: str, body: bytes) -> bytes:
    return (
        b"Message-ID: <" + msgid.encode() + b">\r\n"
        b"From: a@b.example\r\n"
        b"Subject: t\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        b"\r\n" + body
    )


def _ingest_articles_without_trailers(seeded_db, tmp_path, *bodies):
    """Ingest via the real pipeline, then DELETE the ArticleTrailer
    rows so backfill has work."""
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
        s.query(ArticleTrailer).delete()
        s.commit()


def test_backfill_indexes_trailer_articles(seeded_db, tmp_path):
    """Pre-extractor articles get re-walked: bodies with attestation
    trailers land ArticleTrailer rows; prose bodies don't.

    `--limit=2` scopes to the freshly-ingested articles (newest-
    first walk; the seeded conftest articles have lower IDs and
    bogus mirror SHAs so they otherwise dominate the `skipped`
    bucket)."""
    _ingest_articles_without_trailers(
        seeded_db, tmp_path, _TRAILER_BODY, _PROSE_BODY,
    )

    result = backfill_article_trailers(limit=2)
    assert result.examined == 2
    assert result.indexed == 1
    assert result.no_trailers == 1
    assert result.failed == 0

    with seeded_db() as s:
        rows = sorted(
            (a.message_id, t.role, t.address, t.address_normalized)
            for a, t in s.execute(
                select(Article, ArticleTrailer)
                .join(ArticleTrailer, ArticleTrailer.article_id == Article.id)
            ).all()
        )
    assert rows == [
        ("m0@example.com", "Acked-by", "bob@kernel.org", "bob@kernel.org"),
        ("m0@example.com", "Reviewed-by", "Alice@Example.COM", "alice@example.com"),
    ]


def test_backfill_is_idempotent_on_rerun(seeded_db, tmp_path):
    _ingest_articles_without_trailers(seeded_db, tmp_path, _TRAILER_BODY)
    backfill_article_trailers(limit=1)
    second = backfill_article_trailers(limit=1)
    assert second.examined == 1
    assert second.skipped == 1
    assert second.indexed == 0

    with seeded_db() as s:
        roles = sorted(
            r.role for r in s.execute(select(ArticleTrailer)).scalars()
        )
    assert roles == ["Acked-by", "Reviewed-by"]


def test_backfill_reprocess_re_extracts(seeded_db, tmp_path):
    _ingest_articles_without_trailers(seeded_db, tmp_path, _TRAILER_BODY)
    backfill_article_trailers(limit=1)
    result = backfill_article_trailers(limit=1, reprocess=True)
    assert result.examined == 1
    assert result.indexed == 1
    assert result.skipped == 0


def test_backfill_cli_prints_summary(seeded_db, tmp_path):
    _ingest_articles_without_trailers(
        seeded_db, tmp_path, _TRAILER_BODY, _PROSE_BODY,
    )
    result = CliRunner().invoke(
        backfill_article_trailers_command, ["--limit", "2"],
    )
    assert result.exit_code == 0, result.output
    assert "examined=2" in result.output
    assert "indexed=1" in result.output
    assert "no_trailers=1" in result.output


def test_backfill_skips_articles_with_unreachable_mirror(seeded_db):
    result = backfill_article_trailers()
    assert result.failed == 0
    assert result.skipped > 0
