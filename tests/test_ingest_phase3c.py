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


def test_submit_patch_series_batch_updates_columns(writer, seeded_db):
    """Happy path: payload's three columns land on the addressed
    Article row. Other articles are untouched."""
    from sqlalchemy import select

    from mimir._pending_backfill import (
        _PatchSeriesPending,
        _submit_patch_series_batch,
    )
    from mimir.models import Article

    with seeded_db() as s:
        rows = (
            s.execute(select(Article.id).order_by(Article.id).limit(2)).scalars().all()
        )
        a, b = rows

    payloads = [
        _PatchSeriesPending(
            article_id=a,
            patch_series_key="fs/foo-v2",
            patch_series_version="v2",
            patch_series_position=0,
        ),
        _PatchSeriesPending(
            article_id=b,
            patch_series_key="fs/foo-v2",
            patch_series_version="v2",
            patch_series_position=1,
        ),
    ]
    _submit_patch_series_batch(writer, payloads).result(timeout=10)

    with seeded_db() as s:
        results = s.execute(
            select(
                Article.id,
                Article.patch_series_key,
                Article.patch_series_version,
                Article.patch_series_position,
            )
            .where(Article.id.in_([a, b]))
            .order_by(Article.id)
        ).all()
    assert results == [
        (a, "fs/foo-v2", "v2", 0),
        (b, "fs/foo-v2", "v2", 1),
    ]


def test_submit_patch_series_batch_nulls_overwrite_set_values(writer, seeded_db):
    """`patch_series_key=None` overwrites a previously-set value, mirror
    of the orphan-clear path in `_process_one`."""
    from sqlalchemy import select, update

    from mimir._pending_backfill import (
        _PatchSeriesPending,
        _submit_patch_series_batch,
    )
    from mimir.models import Article

    with seeded_db() as s:
        article_id = s.execute(select(Article.id).limit(1)).scalar_one()
        s.execute(
            update(Article)
            .where(Article.id == article_id)
            .values(
                patch_series_key="old-key",
                patch_series_version="v1",
                patch_series_position=2,
            )
        )
        s.commit()

    payloads = [
        _PatchSeriesPending(
            article_id=article_id,
            patch_series_key=None,
            patch_series_version=None,
            patch_series_position=None,
        )
    ]
    _submit_patch_series_batch(writer, payloads).result(timeout=10)

    with seeded_db() as s:
        row = s.execute(
            select(
                Article.patch_series_key,
                Article.patch_series_version,
                Article.patch_series_position,
            ).where(Article.id == article_id)
        ).one()
    assert row == (None, None, None)


def test_submit_canonical_batch_updates_canonical_and_observations(writer, seeded_db):
    """Happy path: payload's canonical id lands on the article; observation
    deltas land in inbox_address_observations with count + last_seen
    aggregated via the UPSERT."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from mimir._pending_backfill import (
        _CanonicalPending,
        _submit_canonical_batch,
    )
    from mimir.models import Article, Inbox, InboxAddressObservation

    with seeded_db() as s:
        article_id = s.execute(select(Article.id).limit(1)).scalar_one()
        alpha_id = s.execute(select(Inbox.id).where(Inbox.name == "alpha")).scalar_one()

    ts = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    payloads = [
        _CanonicalPending(
            article_id=article_id,
            new_canonical_inbox_id=alpha_id,
            observation_deltas={
                alpha_id: {"list@example.com": (1, ts)},
            },
        )
    ]
    _submit_canonical_batch(writer, payloads).result(timeout=10)

    with seeded_db() as s:
        canonical = s.execute(
            select(Article.canonical_inbox_id).where(Article.id == article_id)
        ).scalar_one()
        obs = s.execute(
            select(
                InboxAddressObservation.count,
                InboxAddressObservation.last_seen,
            ).where(
                InboxAddressObservation.inbox_id == alpha_id,
                InboxAddressObservation.address == "list@example.com",
            )
        ).one_or_none()

    assert canonical == alpha_id
    assert obs is not None
    assert obs.count >= 1


def test_submit_canonical_batch_no_canonical_change_skips_update(writer, seeded_db):
    """When the resolved canonical equals what's already there, the
    closure's UPDATE is a no-op (no row churn). Observations still land."""
    from datetime import datetime, timezone

    from sqlalchemy import select, update

    from mimir._pending_backfill import (
        _CanonicalPending,
        _submit_canonical_batch,
    )
    from mimir.models import Article, Inbox, InboxAddressObservation

    with seeded_db() as s:
        article_id = s.execute(select(Article.id).limit(1)).scalar_one()
        alpha_id = s.execute(select(Inbox.id).where(Inbox.name == "alpha")).scalar_one()
        s.execute(
            update(Article)
            .where(Article.id == article_id)
            .values(canonical_inbox_id=alpha_id)
        )
        s.commit()

    ts = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    payloads = [
        _CanonicalPending(
            article_id=article_id,
            new_canonical_inbox_id=alpha_id,
            observation_deltas={alpha_id: {"a@x.example": (2, ts)}},
        )
    ]
    _submit_canonical_batch(writer, payloads).result(timeout=10)

    with seeded_db() as s:
        obs_count = s.execute(
            select(InboxAddressObservation.count).where(
                InboxAddressObservation.inbox_id == alpha_id,
                InboxAddressObservation.address == "a@x.example",
            )
        ).scalar_one()
    assert obs_count == 2


def test_submit_promote_list_address_sweep_promotes_eligible_inbox(writer, seeded_db):
    """The sweep helper runs the promotion check for every inbox at
    NULL `list_address`. An eligible inbox (top_count >= threshold
    AND top/(top+second) >= dominance) gets its `list_address` set."""
    from datetime import datetime, timezone

    from sqlalchemy import insert, select, update

    from mimir._pending_backfill import _submit_promote_list_address_sweep
    from mimir.ingest.epoch import MIN_PROMOTE_OBSERVATIONS
    from mimir.models import Inbox, InboxAddressObservation

    with seeded_db() as s:
        beta_id = s.execute(select(Inbox.id).where(Inbox.name == "beta")).scalar_one()
        s.execute(update(Inbox).where(Inbox.id == beta_id).values(list_address=None))
        ts = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
        s.execute(
            insert(InboxAddressObservation).values(
                inbox_id=beta_id,
                address="dominant@vger.kernel.org",
                count=MIN_PROMOTE_OBSERVATIONS + 10,
                last_seen=ts,
            )
        )
        s.commit()

    _submit_promote_list_address_sweep(writer).result(timeout=10)

    with seeded_db() as s:
        post = s.execute(
            select(Inbox.list_address).where(Inbox.id == beta_id)
        ).scalar_one()
    assert post == "dominant@vger.kernel.org"


def test_backfill_article_files_uses_writer_thread_via_active_context(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    """Phase 3c contract: `backfill_article_files` must NOT call
    `write_transaction`. Pre-3c held a write_transaction for the
    whole walk; Phase 3c dispatches via the active WriterThread.
    Monkeypatching `write_transaction` to raise asserts the new
    code path never reaches it.
    """
    import mimir.extensions as ext_mod

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "write_transaction must not be called by "
            "backfill_article_files after Phase 3c"
        )

    monkeypatch.setattr(ext_mod, "write_transaction", _forbidden)

    from mimir.patches import backfill_article_files

    result = backfill_article_files(limit=10)
    assert result.examined > 0, (
        "the test relies on the walker visiting at least one article; "
        "if it returned 0, the structural assertion is meaningless"
    )


def test_backfill_article_trailers_uses_writer_thread_via_active_context(
    seeded_db, monkeypatch, broker_active
):
    """Phase 3c contract: `backfill_article_trailers` must NOT call
    `write_transaction`."""
    import mimir.extensions as ext_mod

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "write_transaction must not be called by "
            "backfill_article_trailers after Phase 3c"
        )

    monkeypatch.setattr(ext_mod, "write_transaction", _forbidden)

    from mimir.trailers import backfill_article_trailers

    result = backfill_article_trailers(limit=10)
    assert result.examined > 0
