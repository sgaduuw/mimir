"""Phase 3c tests: per-batch composite WriteOp dispatch in the four
patch-metadata backfills.

Pins the structural contract for Phase 3c of the broker two-pool
restructure (`_claude/specs/2026-05-29-broker-two-pool-design.md`).
Kept in its own file so the Phase 3c PR audit is easy.
"""

from mimir._pending_backfill import (
    _ArticleFilesPending,
    _ArticleTrailersPending,
    _CanonicalPending,
    _PatchSeriesPending,
)


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
