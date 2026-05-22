"""Cross-cutting ingest tests: alembic migration backfills
that span ingest concerns (the `last_article_date` MAX
rollup, the `patch_series_position=0` cover-letter
backfill). Filed here rather than in any one ingest
submodule because the SQL runs against the schema, not
through the ingest pipeline."""

from datetime import datetime, timezone

from sqlalchemy import select

from mimir.models import (
    Article,
    Inbox,
)

from tests.test_ingest._helpers import _naive_utc


def test_migration_backfill_sql_populates_last_article_date(seeded_db):
    """The alembic backfill statement (correlated MAX(date) per inbox)
    populates `Inbox.last_article_date` from existing articles +
    article_lists. Pins the upgrade-against-populated-DB path without
    re-running alembic, the conftest seed is a stand-in for a populated
    prod row set.

    Conftest seeds alpha with art1/art3/art4 (max 2024-03-01) and
    beta with art2/art3 (max 2024-03-01). Wipe the column first to
    simulate the pre-upgrade state, then run the migration's backfill
    SQL inline and verify both inboxes pick up the right max."""
    from datetime import datetime
    from sqlalchemy import text

    with seeded_db() as s:
        # Simulate the pre-upgrade state.
        s.execute(text("UPDATE inboxes SET last_article_date = NULL"))
        # Verbatim from alembic/versions/...
        s.execute(
            text(
                """
            UPDATE inboxes
            SET last_article_date = (
                SELECT MAX(a.date)
                FROM articles a
                JOIN article_lists al ON al.article_id = a.id
                WHERE al.inbox_id = inboxes.id
            )
            """
            )
        )
        s.commit()
    with seeded_db() as s:
        rows = s.execute(select(Inbox.name, Inbox.last_article_date)).all()
    by_name = {n: d for n, d in rows}
    assert _naive_utc(by_name["alpha"]) == datetime(2024, 3, 1, 12, 0)
    assert _naive_utc(by_name["beta"]) == datetime(2024, 3, 1, 12, 0)


def test_migration_backfill_sets_position_zero_on_keyed_covers(seeded_db):
    """The #212 alembic backfill statement marks every article that
    already has `patch_series_key` set as `patch_series_position=0`
    (Slice 1 only keyed covers). Pins the upgrade-against-populated-DB
    path without re-running alembic."""
    from sqlalchemy import text

    with seeded_db() as s:
        # Seed a "pre-#212" cover: key set, position NULL.
        cover = Article(
            message_id="legacy-cv@x",
            subject="[PATCH 0/2] legacy",
            author="A <a@x>",
            date=datetime(2024, 5, 1, tzinfo=timezone.utc),
            thread_parent=None,
            subject_normalized="legacy",
            patch_series_key="legacy",
            patch_series_version="v1",
            patch_series_position=None,
        )
        s.add(cover)
        s.commit()
        # Verbatim from alembic/versions/...patch_series_position.py
        s.execute(
            text(
                """
            UPDATE articles
            SET patch_series_position = 0
            WHERE patch_series_key IS NOT NULL
            """
            )
        )
        s.commit()
    with seeded_db() as s:
        reloaded = s.execute(
            select(Article).where(Article.message_id == "legacy-cv@x")
        ).scalar_one()
    assert reloaded.patch_series_position == 0
