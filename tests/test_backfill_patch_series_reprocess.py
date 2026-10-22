"""#816: `backfill-patch-series --reprocess` is the documented way to
rewrite stored versions after the `[PATCHv2 ...]` parser fix. One pass
must leave the in-series patches on the same version as their cover."""

from datetime import UTC, datetime

from sqlalchemy import select

from mimir.models import Article, ArticleList, Inbox
from mimir.patch_series import backfill_patch_series, series_key


def _add(seeded_db, msgid, subject, position, parent=None):
    """Row as the pre-#816 parser left it: keyed, but stored as v1."""
    with seeded_db() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.add(
            Article(
                message_id=msgid,
                subject=subject,
                author="A <a@x>",
                date=datetime(2024, 6, 1, tzinfo=UTC),
                thread_parent=parent,
                subject_normalized=subject.lower(),
                canonical_inbox_id=inbox.id,
                lists=[
                    ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="d" * 40)
                ],
                patch_series_key=series_key("improve foo", "A <a@x>"),
                patch_series_version="v1",
                patch_series_position=position,
            )
        )
        s.commit()


def test_reprocess_moves_glued_series_to_v2_in_one_pass(seeded_db, broker_active):
    _add(seeded_db, "cover816@x", "[PATCHv2 0/2] improve foo", 0)
    _add(seeded_db, "p1-816@x", "[PATCHv2 1/2] foo: add bar", 1, "cover816@x")
    _add(seeded_db, "p2-816@x", "[PATCHv2 2/2] foo: add baz", 2, "cover816@x")

    backfill_patch_series(reprocess=True)

    with seeded_db() as s:
        versions = dict(
            s.execute(
                select(Article.message_id, Article.patch_series_version).where(
                    Article.message_id.in_(["cover816@x", "p1-816@x", "p2-816@x"])
                )
            ).all()
        )
    assert versions == {"cover816@x": "v2", "p1-816@x": "v2", "p2-816@x": "v2"}
