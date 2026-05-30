"""Phase 3c tests: per-batch composite WriteOp dispatch in the four
patch-metadata backfills.

Pins the structural contract for Phase 3c of the broker two-pool
restructure (`_claude/specs/2026-05-29-broker-two-pool-design.md`).
Kept in its own file so the Phase 3c PR audit is easy.
"""

import pytest
from sqlalchemy import insert, select

from mimir._pending_backfill import (
    _ArticleFilesPending,
    _ArticleTrailersPending,
    _CanonicalPending,
    _PatchSeriesPending,
    _submit_article_files_batch,
)
from mimir.broker.writes import WriterThread
from mimir.models import Article, ArticleFile


def test_article_files_pending_default_shape():
    p = _ArticleFilesPending(article_id=1)
    assert p.article_id == 1
    assert p.delete_first is False
    assert p.paths == []


def test_article_trailers_pending_default_shape():
    p = _ArticleTrailersPending(article_id=1)
    assert p.article_id == 1
    assert p.delete_first is False
    assert p.trailers == []


def test_patch_series_pending_default_shape():
    p = _PatchSeriesPending(
        article_id=1,
        patch_series_key=None,
        patch_series_version=None,
        patch_series_position=None,
    )
    assert p.article_id == 1
    assert p.patch_series_key is None
    assert p.patch_series_version is None
    assert p.patch_series_position is None


def test_canonical_pending_default_shape():
    p = _CanonicalPending(
        article_id=1,
        new_canonical_inbox_id=None,
        observation_deltas={},
    )
    assert p.article_id == 1
    assert p.new_canonical_inbox_id is None
    assert p.observation_deltas == {}


@pytest.fixture
def writer():
    """Function-scoped WriterThread for _submit_*_batch tests.

    Mirrors the `writer` fixture in `tests/test_ingest_phase3b.py`. The
    autouse `_reset_db` fixture seeds the DB before this fixture is
    entered, so writes against the seeded `articles` rows are safe.
    """
    wt = WriterThread.from_settings()
    wt.start()
    yield wt
    wt.stop(timeout=10)


def test_submit_article_files_batch_inserts_rows(writer, seeded_db):
    """Happy path: 2 articles' worth of pending files lands 4 rows."""

    with seeded_db() as s:
        rows = (
            s.execute(select(Article.id).order_by(Article.id).limit(2)).scalars().all()
        )
        a, b = rows

    payloads = [
        _ArticleFilesPending(article_id=a, paths=["fs/foo/a.c", "fs/foo/b.c"]),
        _ArticleFilesPending(article_id=b, paths=["arch/x86/y.c", "arch/x86/z.c"]),
    ]
    _submit_article_files_batch(writer, payloads).result(timeout=10)

    with seeded_db() as s:
        out = s.execute(
            select(ArticleFile.article_id, ArticleFile.path)
            .where(ArticleFile.article_id.in_([a, b]))
            .order_by(ArticleFile.article_id, ArticleFile.path)
        ).all()
    assert set(out) == {
        (a, "fs/foo/a.c"),
        (a, "fs/foo/b.c"),
        (b, "arch/x86/y.c"),
        (b, "arch/x86/z.c"),
    }


def test_submit_article_files_batch_delete_first_replaces_rows(writer, seeded_db):
    """`delete_first=True` removes existing rows for the article before
    inserting the new paths, mirror of the `--reprocess` semantics."""

    with seeded_db() as s:
        article_id = s.execute(select(Article.id).limit(1)).scalar_one()
        s.execute(insert(ArticleFile).values(article_id=article_id, path="stale/old.c"))
        s.commit()

    payloads = [
        _ArticleFilesPending(
            article_id=article_id, delete_first=True, paths=["fresh/new.c"]
        )
    ]
    _submit_article_files_batch(writer, payloads).result(timeout=10)

    with seeded_db() as s:
        out = (
            s.execute(
                select(ArticleFile.path)
                .where(ArticleFile.article_id == article_id)
                .order_by(ArticleFile.path)
            )
            .scalars()
            .all()
        )
    assert out == ["fresh/new.c"], (
        "delete_first should have wiped the stale row; only the fresh path survives"
    )


def test_submit_article_files_batch_empty_is_noop(writer):
    """Empty payload list returns a pre-resolved future without touching
    the writer queue."""
    future = _submit_article_files_batch(writer, [])
    future.result(timeout=1)


def test_submit_article_trailers_batch_inserts_rows(writer, seeded_db):
    """Happy path: 1 article with 2 trailers lands 2 rows with
    address_normalized populated from lowercased address."""
    from sqlalchemy import select

    from mimir._pending_backfill import (
        _ArticleTrailerInsert,
        _ArticleTrailersPending,
        _submit_article_trailers_batch,
    )
    from mimir.models import Article, ArticleTrailer

    with seeded_db() as s:
        article_id = s.execute(select(Article.id).limit(1)).scalar_one()

    payloads = [
        _ArticleTrailersPending(
            article_id=article_id,
            trailers=[
                _ArticleTrailerInsert(
                    role="Reviewed-by", name="Alice", address="Alice@Example.com"
                ),
                _ArticleTrailerInsert(
                    role="Signed-off-by", name="Bob", address="bob@example.com"
                ),
            ],
        )
    ]
    _submit_article_trailers_batch(writer, payloads).result(timeout=10)

    with seeded_db() as s:
        rows = s.execute(
            select(
                ArticleTrailer.role,
                ArticleTrailer.name,
                ArticleTrailer.address,
                ArticleTrailer.address_normalized,
            )
            .where(ArticleTrailer.article_id == article_id)
            .order_by(ArticleTrailer.name)
        ).all()
    assert rows == [
        ("Reviewed-by", "Alice", "Alice@Example.com", "alice@example.com"),
        ("Signed-off-by", "Bob", "bob@example.com", "bob@example.com"),
    ]


def test_submit_article_trailers_batch_delete_first_replaces_rows(writer, seeded_db):
    """`delete_first=True` removes existing trailer rows for the article
    before inserting fresh ones, mirror of the `--reprocess` semantics."""
    from sqlalchemy import insert, select

    from mimir._pending_backfill import (
        _ArticleTrailerInsert,
        _ArticleTrailersPending,
        _submit_article_trailers_batch,
    )
    from mimir.models import Article, ArticleTrailer

    with seeded_db() as s:
        article_id = s.execute(select(Article.id).limit(1)).scalar_one()
        s.execute(
            insert(ArticleTrailer).values(
                article_id=article_id,
                role="Reviewed-by",
                name="Stale",
                address="stale@example.com",
                address_normalized="stale@example.com",
            )
        )
        s.commit()

    payloads = [
        _ArticleTrailersPending(
            article_id=article_id,
            delete_first=True,
            trailers=[
                _ArticleTrailerInsert(
                    role="Tested-by", name="Fresh", address="fresh@example.com"
                )
            ],
        )
    ]
    _submit_article_trailers_batch(writer, payloads).result(timeout=10)

    with seeded_db() as s:
        rows = (
            s.execute(
                select(ArticleTrailer.name)
                .where(ArticleTrailer.article_id == article_id)
                .order_by(ArticleTrailer.name)
            )
            .scalars()
            .all()
        )
    assert rows == ["Fresh"], (
        "delete_first should have wiped the stale row; only Fresh survives"
    )
