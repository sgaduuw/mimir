"""Both backfills read the canonical inbox's copy first when the article
is filed there. The reader falls through to other filed inboxes, so a
read that succeeds proves nothing about order; this spies on which
inbox is asked first."""

import pytest
from sqlalchemy import select

from mimir import store
from mimir.models import Article, ArticleList, ArticleTrailer, Inbox
from mimir.patches import backfill_article_files
from mimir.trailers import backfill_article_trailers
from tests.test_cli_backfill_article_files import (
    _PATCH_BODY,
    _ingest_articles_without_files,
)


@pytest.mark.parametrize(
    "backfill", [backfill_article_files, backfill_article_trailers]
)
def test_backfill_asks_canonical_inbox_first(
    backfill, seeded_db, tmp_path, broker_active, monkeypatch
):
    _ingest_articles_without_files(seeded_db, tmp_path, _PATCH_BODY)
    with seeded_db() as s:
        # zeta sorts after alpha, so name order alone would ask alpha first.
        zeta = Inbox(name="zeta", mirror_path=str(tmp_path), upstream_url="u/z")
        s.add(zeta)
        s.flush()
        art = s.execute(
            select(Article).where(Article.message_id == "m0@example.com")
        ).scalar_one()
        (alpha_link,) = art.lists
        s.add(
            ArticleList(
                article_id=art.id,
                inbox_id=zeta.id,
                epoch=alpha_link.epoch,
                commit_sha=alpha_link.commit_sha,
            )
        )
        art.canonical_inbox_id = zeta.id
        s.query(ArticleTrailer).delete()  # so the trailers walk reads too
        s.commit()

    asked = []
    real = store.read_message

    def _spy(session, inbox, message_id):
        asked.append(inbox.name)
        return real(session, inbox, message_id)

    monkeypatch.setattr(store, "read_message", _spy)
    result = backfill(limit=1)
    assert result.examined == 1  # precondition: m0 is the one walked
    assert asked == ["zeta"]
