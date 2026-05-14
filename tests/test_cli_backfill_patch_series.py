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
            message_id=msgid, subject=subject, author=author,
            date=datetime(2024, 6, 1, tzinfo=timezone.utc),
            thread_parent=None, subject_normalized=subject.lower(),
            canonical_inbox_id=inbox.id,
            lists=[ArticleList(inbox_id=inbox.id, epoch="0.git",
                               commit_sha="f" * 40)],
        )
        s.add(art)
        s.commit()
        return art.id


def test_backfill_indexes_cover_letters(seeded_db):
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


def test_backfill_is_idempotent_on_rerun(seeded_db):
    """Second run with no new articles → cover-letter rows are
    `skipped`, non-cover-letters are `not_cover` again. No
    duplicate key writes."""
    _add_article(seeded_db, "cover@x", "[PATCH v2 0/3] improve foo")
    first = backfill_patch_series()
    second = backfill_patch_series()
    assert first.indexed == 1
    assert second.indexed == 0
    assert second.skipped == 1   # cover@x already has a key


def test_backfill_reprocess_clears_stale_keys(seeded_db):
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


def test_backfill_cli_prints_summary(seeded_db):
    _add_article(seeded_db, "c@x", "[PATCH 0/3] improve foo")
    result = CliRunner().invoke(backfill_patch_series_command, [])
    assert result.exit_code == 0, result.output
    assert "indexed=1" in result.output


def test_backfill_cli_honours_limit(seeded_db):
    _add_article(seeded_db, "a@x", "[PATCH 0/3] one")
    _add_article(seeded_db, "b@x", "[PATCH 0/3] two")
    _add_article(seeded_db, "c@x", "[PATCH 0/3] three")
    result = CliRunner().invoke(backfill_patch_series_command, ["--limit", "2"])
    assert result.exit_code == 0
    assert "examined=2" in result.output
