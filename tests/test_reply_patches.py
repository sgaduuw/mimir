"""A reply that repeats a patch's subject without "Re:" is not a patch
(#837).

The in-series counterpart of #820: reviewers answer a patch with its
subject as is. Such a reply carries no diff, which is what tells it
from a re-post of the patch by someone else.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from mimir.models import Article, ArticleFile, ArticleList, Inbox
from mimir.patch_revisions import parse_in_series_patch_subject
from mimir.patch_series import (
    _reply_to_patch_in_db,
    backfill_patch_series,
    series_key,
)
from tests._helpers import PRODUCTION_STATS, plan_of, planner_stats, series_columns
from tests.test_ingest.test_reply_covers import _ingest, _msg

COVER = "[PATCH v2 0/2] improve foo handling"
PATCH = "[PATCH v2 1/2] foo: add\tbar"
REPLY = "[PATCH v2 1/2] foo: add bar"
DIFF = "diff --git a/foo.c b/foo.c\n--- a/foo.c\n+++ b/foo.c\n@@ -1 +1 @@\n-a\n+b\n"
KEY = series_key("improve foo handling", "a@b.example")

_series = series_columns


def _with_body(raw: bytes, body: str) -> bytes:
    return raw.replace(b"\r\n\r\nhello", b"\r\n\r\n" + body.encode())


@pytest.mark.parametrize("batches", [1, 2])
def test_ingest_and_reprocess_store_a_patch_reply_without_a_series(
    seeded_db, broker_active, tmp_path, batches
):
    """Same batch (the parent is found in the pending list) and across
    batches (found by one select on message_id), then `--reprocess`."""
    # Precondition: the reply's subject alone reads as patch 1/2.
    assert parse_in_series_patch_subject(REPLY).position == 1
    cover = _msg("c837@x", COVER)
    patch = _with_body(_msg("p837@x", PATCH, in_reply_to="c837@x"), DIFF)
    reply = _msg("r837@x", REPLY, sender="rev@b.example", in_reply_to="p837@x")
    # Guard: someone else re-posting the patch, diff included.
    repost = _with_body(
        _msg("rp837@x", REPLY, sender="co@b.example", in_reply_to="p837@x"), DIFF
    )
    if batches == 1:
        _ingest(seeded_db, tmp_path, "0.git", [cover, patch, reply, repost])
    else:
        _ingest(seeded_db, tmp_path, "0.git", [cover, patch])
        _ingest(seeded_db, tmp_path, "1.git", [reply, repost])
    ids = ("c837@x", "p837@x", "r837@x", "rp837@x")

    ingested = _series(seeded_db, *ids)
    assert ingested["p837@x"] == (KEY, "v2", 1, None)
    assert ingested["r837@x"] == (None, None, None, None)
    assert ingested["rp837@x"] == (KEY, "v2", 1, None)

    result = backfill_patch_series(reprocess=True)

    reprocessed = _series(seeded_db, *ids)
    # A reprocess links only through a cover parent, so it orphans the
    # re-post ingest linked through the patch (#840); its position stays.
    assert reprocessed["rp837@x"] == (None, None, 1, None)
    del ingested["rp837@x"], reprocessed["rp837@x"]
    assert reprocessed == ingested
    assert result.reply_patch == 1


def _add(seeded_db, msgid, subject, author, *, parent=None, key=None, position=None):
    with seeded_db() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        article = Article(
            message_id=msgid,
            subject=subject,
            author=author,
            date=datetime(2024, 6, 1, tzinfo=UTC),
            thread_parent=parent,
            subject_normalized=subject.lower(),
            canonical_inbox_id=inbox.id,
            lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="d" * 40)],
            patch_series_key=key,
            patch_series_version="v2" if key else None,
            patch_series_position=position,
        )
        s.add(article)
        s.commit()
        return article.id


def _touch(seeded_db, article_id):
    with seeded_db() as s:
        s.add(ArticleFile(article_id=article_id, path="foo.c"))
        s.commit()


def test_a_plain_run_clears_a_stored_patch_reply(seeded_db, broker_active):
    """Stored as every reply is today: position set, no key. A plain run
    clears it, and the next plain run meets it again rather than
    counting it resolved."""
    author = "Al <a@x>"
    key = series_key("improve foo handling", author)
    _add(seeded_db, "c837@x", COVER, author, key=key, position=0)
    patch = _add(
        seeded_db, "p837@x", PATCH, author, parent="c837@x", key=key, position=1
    )
    _add(seeded_db, "r837@x", REPLY, "Rev <rev@x>", parent="p837@x", position=1)
    repost = _add(seeded_db, "rp837@x", REPLY, "Co <co@x>", parent="p837@x", position=1)
    _touch(seeded_db, patch)
    _touch(seeded_db, repost)

    first = backfill_patch_series()
    second = backfill_patch_series()

    got = _series(seeded_db, "r837@x", "rp837@x")
    assert got["r837@x"] == (None, None, None, None)
    # Guard: the re-post touches files, so it is linked as a patch.
    assert got["rp837@x"] == (key, "v2", 1, None)
    assert (first.reply_patch, second.reply_patch) == (1, 1)


def test_reprocess_clears_a_keyed_patch_reply(seeded_db, broker_active):
    """The upgrade path: a reply ingested before this fix carries its
    patch's key. A plain run skips it as resolved; `--reprocess` clears
    it, and the patch and cover keep their key."""
    author = "Al <a@x>"
    key = series_key("improve foo handling", author)
    _add(seeded_db, "c837@x", COVER, author, key=key, position=0)
    patch = _add(
        seeded_db, "p837@x", PATCH, author, parent="c837@x", key=key, position=1
    )
    _touch(seeded_db, patch)
    _add(
        seeded_db, "r837@x", REPLY, "Rev <rev@x>", parent="p837@x", key=key, position=1
    )

    plain = backfill_patch_series()

    assert plain.reply_patch == 0
    assert _series(seeded_db, "r837@x")["r837@x"][:3] == (key, "v2", 1)

    result = backfill_patch_series(reprocess=True)

    got = _series(seeded_db, "c837@x", "p837@x", "r837@x")
    assert got["r837@x"] == (None, None, None, None)
    assert got["p837@x"][:3] == (key, "v2", 1)
    assert got["c837@x"][:3] == (key, "v2", 0)
    assert result.reply_patch == 1


def test_the_diff_lookup_seeks_the_article_files_key(seeded_db):
    """The backfill asks once per in-series row whether it touches files;
    on a full archive that must seek the primary key, not scan."""
    reply = _add(seeded_db, "r837@x", REPLY, "Rev <rev@x>", parent="p837@x")
    _add(seeded_db, "p837@x", PATCH, "Al <a@x>")
    # Production's article_files rows, read 2026-10-24.
    stats = (
        *PRODUCTION_STATS,
        ("article_files", "ix_article_files_path", "15194624 24"),
        ("article_files", "sqlite_autoindex_article_files_1", "15194624 3 1"),
    )
    with planner_stats(seeded_db, stats), seeded_db() as s:
        article = s.get(Article, reply)
        plan = plan_of(
            s,
            lambda: _reply_to_patch_in_db(s, article, REPLY),
            "FROM article_files",
        )
    assert "SEARCH article_files USING COVERING INDEX" in plan, plan
    assert "sqlite_autoindex_article_files_1 (article_id=?)" in plan, plan
