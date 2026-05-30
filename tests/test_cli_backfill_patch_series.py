"""CLI + service test for `backfill-patch-series`. The parser is
exercised in `tests/test_patch_series.py`; this pins the walker's
idempotence + the bucket counters."""

from click.testing import CliRunner
from sqlalchemy import select

from mimir.cli import backfill_patch_series_command
from mimir.models import Article, ArticleList, Inbox
from mimir.patch_series import backfill_patch_series


def _add_article(seeded_db, msgid, subject, author="A <a@x>"):
    """Insert a minimal Article with no series-key set, so the
    backfill has something to do. Linked to the seeded `alpha`
    inbox for the route-side compatibility shape."""
    from datetime import datetime, timezone

    with seeded_db() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        art = Article(
            message_id=msgid,
            subject=subject,
            author=author,
            date=datetime(2024, 6, 1, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized=subject.lower(),
            canonical_inbox_id=inbox.id,
            lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="f" * 40)],
        )
        s.add(art)
        s.commit()
        return art.id


def test_backfill_indexes_cover_letters(seeded_db, broker_active):
    """Pre-detector articles get re-walked: cover-letter subjects
    land keys, non-cover-letter subjects don't."""
    _add_article(seeded_db, "cover@x", "[PATCH v2 0/3] improve foo")
    _add_article(seeded_db, "patch@x", "[PATCH v2 1/3] foo: add bar")
    _add_article(seeded_db, "prose@x", "just a discussion")

    result = backfill_patch_series()
    # Plus the seeded fixture articles, none of which are cover
    # letters; assertions filter to the ones we added.
    assert result.indexed == 1
    assert result.not_cover >= 2  # patch + prose, plus seeded ones

    with seeded_db() as s:
        cover = s.execute(
            select(Article).where(Article.message_id == "cover@x")
        ).scalar_one()
        patch = s.execute(
            select(Article).where(Article.message_id == "patch@x")
        ).scalar_one()
    assert cover.patch_series_version == "v2"
    assert cover.patch_series_key is not None
    assert patch.patch_series_key is None


def test_backfill_is_idempotent_on_rerun(seeded_db, broker_active):
    """Second run with no new articles → cover-letter rows are
    `skipped`, non-cover-letters are `not_cover` again. No
    duplicate key writes."""
    _add_article(seeded_db, "cover@x", "[PATCH v2 0/3] improve foo")
    first = backfill_patch_series()
    second = backfill_patch_series()
    assert first.indexed == 1
    assert second.indexed == 0
    assert second.skipped == 1  # cover@x already has a key


def test_backfill_reprocess_clears_stale_keys(seeded_db, broker_active):
    """`--reprocess` clears keys on articles whose subject no
    longer parses as a cover letter. Useful after a parser
    regression fix."""
    art_id = _add_article(seeded_db, "stale@x", "[PATCH 0/3] foo bar")
    backfill_patch_series()
    # Mutate the subject so it no longer looks like a cover letter.
    with seeded_db() as s:
        a = s.get(Article, art_id)
        a.subject = "Re: [PATCH 0/3] foo bar"  # reply, not a cover letter
        s.commit()
    result = backfill_patch_series(reprocess=True)
    assert result.not_cover >= 1
    with seeded_db() as s:
        a = s.get(Article, art_id)
    assert a.patch_series_key is None
    assert a.patch_series_version is None


def test_backfill_cli_prints_summary(seeded_db, broker_active):
    _add_article(seeded_db, "c@x", "[PATCH 0/3] improve foo")
    result = CliRunner().invoke(backfill_patch_series_command, [])
    assert result.exit_code == 0, result.output
    assert "indexed=1" in result.output


def test_backfill_cli_honours_limit(seeded_db, broker_active):
    _add_article(seeded_db, "a@x", "[PATCH 0/3] one")
    _add_article(seeded_db, "b@x", "[PATCH 0/3] two")
    _add_article(seeded_db, "c@x", "[PATCH 0/3] three")
    result = CliRunner().invoke(backfill_patch_series_command, ["--limit", "2"])
    assert result.exit_code == 0
    assert "examined=2" in result.output


# #212 (position + in-series linkage)


def _add_article_with_parent(seeded_db, msgid, subject, parent_msgid):
    """Like `_add_article` but with `thread_parent` set so the
    backfill's in-series linker can walk up to the cover."""
    from datetime import datetime, timezone

    with seeded_db() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        art = Article(
            message_id=msgid,
            subject=subject,
            author="A <a@x>",
            date=datetime(2024, 6, 1, tzinfo=timezone.utc),
            thread_parent=parent_msgid,
            subject_normalized=subject.lower(),
            canonical_inbox_id=inbox.id,
            lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="e" * 40)],
        )
        s.add(art)
        s.commit()
        return art.id


def test_backfill_sets_position_zero_on_cover_letters(seeded_db, broker_active):
    """A cover letter walked by backfill lands with
    `patch_series_position = 0`, the column #212 added."""
    _add_article(seeded_db, "cv@x", "[PATCH v2 0/3] improve foo")
    backfill_patch_series()
    with seeded_db() as s:
        cover = s.execute(
            select(Article).where(Article.message_id == "cv@x")
        ).scalar_one()
    assert cover.patch_series_position == 0
    assert cover.patch_series_key is not None


def test_backfill_links_in_series_via_thread_parent(seeded_db, broker_active):
    """In-series patch with `thread_parent` set to a cover letter
    that's already backfilled inherits the cover's `(key, version)`
    and lands in the `in_series_indexed` bucket."""
    _add_article(seeded_db, "cover@x", "[PATCH v2 0/3] thread title")
    _add_article_with_parent(
        seeded_db,
        "patch1of3@x",
        "[PATCH v2 1/3] thread title: first patch",
        parent_msgid="cover@x",
    )
    result = backfill_patch_series()
    assert result.indexed == 1
    assert result.in_series_indexed == 1
    with seeded_db() as s:
        cover = s.execute(
            select(Article).where(Article.message_id == "cover@x")
        ).scalar_one()
        patch = s.execute(
            select(Article).where(Article.message_id == "patch1of3@x")
        ).scalar_one()
    assert patch.patch_series_position == 1
    assert patch.patch_series_key == cover.patch_series_key
    assert patch.patch_series_version == cover.patch_series_version


def test_backfill_in_series_orphan_when_cover_missing(seeded_db, broker_active):
    """In-series patch without a thread_parent pointing at an
    already-keyed cover lands in the `in_series_orphan` bucket:
    position set, key + version stay NULL."""
    _add_article(seeded_db, "patch-orphan@x", "[PATCH 1/3] orphan title")
    result = backfill_patch_series()
    assert result.in_series_orphan == 1
    with seeded_db() as s:
        patch = s.execute(
            select(Article).where(Article.message_id == "patch-orphan@x")
        ).scalar_one()
    assert patch.patch_series_position == 1
    assert patch.patch_series_key is None
    assert patch.patch_series_version is None


def test_backfill_relinks_orphan_when_cover_arrives_later(seeded_db, broker_active):
    """Re-running backfill after a previously-orphaned in-series
    patch's cover lands in the DB links them, without needing
    `--reprocess`. Pins the design: orphans are re-attempted every
    run because their state is "linkage attempted, not found"
    rather than "fully resolved"."""
    # First run: patch is orphaned (cover not yet in DB).
    _add_article_with_parent(
        seeded_db,
        "early-patch@x",
        "[PATCH 1/3] title: first patch",
        parent_msgid="late-cover@x",  # parent not yet in DB
    )
    first = backfill_patch_series()
    assert first.in_series_orphan == 1
    # Cover arrives in a later ingest tick; backfill picks it up.
    _add_article(seeded_db, "late-cover@x", "[PATCH 0/3] title")
    second = backfill_patch_series()
    # Cover indexed; previously-orphaned patch now linked.
    assert second.indexed == 1
    assert second.in_series_indexed == 1
    with seeded_db() as s:
        cover = s.execute(
            select(Article).where(Article.message_id == "late-cover@x")
        ).scalar_one()
        patch = s.execute(
            select(Article).where(Article.message_id == "early-patch@x")
        ).scalar_one()
    assert patch.patch_series_key == cover.patch_series_key
    assert patch.patch_series_version == cover.patch_series_version
