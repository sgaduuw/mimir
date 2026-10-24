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

    ingested = series_columns(seeded_db, *ids)
    assert ingested["p837@x"] == (KEY, "v2", 1, None)
    assert ingested["r837@x"] == (None, None, None, None)
    assert ingested["rp837@x"] == (KEY, "v2", 1, None)

    result = backfill_patch_series(reprocess=True)

    # The re-post answers the patch, not the cover: both paths link it
    # through the patch (#840).
    assert series_columns(seeded_db, *ids) == ingested
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

    got = series_columns(seeded_db, "r837@x", "rp837@x")
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
    assert series_columns(seeded_db, "r837@x")["r837@x"][:3] == (key, "v2", 1)

    result = backfill_patch_series(reprocess=True)

    got = series_columns(seeded_db, "c837@x", "p837@x", "r837@x")
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


# Whoever sends it (#860): the author's own no-diff repeat is a reply.


@pytest.mark.parametrize("batches", [1, 2])
def test_ingest_and_reprocess_clear_a_same_sender_patch_reply(
    seeded_db, broker_active, tmp_path, batches
):
    cover = _msg("c860@x", COVER)
    patch = _with_body(_msg("p860@x", PATCH, in_reply_to="c860@x"), DIFF)
    own = _msg("o860@x", REPLY, in_reply_to="p860@x")
    # A reviewer answering the author's reply (parent is a cleared reply).
    rev = _msg("v860@x", REPLY, sender="rev@b.example", in_reply_to="o860@x")
    rev_again = _msg("w860@x", REPLY, sender="rev@b.example", in_reply_to="v860@x")
    # Guard: the author re-posting the patch with its diff stays a patch.
    repost = _with_body(_msg("rp860@x", REPLY, in_reply_to="p860@x"), DIFF)
    # Guard: a cover from the cover's own sender stays a cover (#820).
    cover_again = _msg("c2860@x", COVER, in_reply_to="c860@x")
    msgs = [cover, patch, own, rev, rev_again, repost, cover_again]
    if batches == 1:
        _ingest(seeded_db, tmp_path, "0.git", msgs)
    else:
        _ingest(seeded_db, tmp_path, "0.git", msgs[:2])
        _ingest(seeded_db, tmp_path, "1.git", msgs[2:])
    ids = ("p860@x", "o860@x", "v860@x", "w860@x", "rp860@x", "c2860@x")

    ingested = series_columns(seeded_db, *ids)
    assert ingested["p860@x"] == (KEY, "v2", 1, None)
    for cleared in ("o860@x", "v860@x", "w860@x"):
        assert ingested[cleared] == (None, None, None, None), cleared
    assert ingested["rp860@x"] == (KEY, "v2", 1, None)
    assert ingested["c2860@x"][:3] == (KEY, "v2", 0)

    backfill_patch_series(reprocess=True)

    assert series_columns(seeded_db, *ids) == ingested


def test_a_plain_run_and_reprocess_clear_a_stored_same_sender_reply(
    seeded_db, broker_active
):
    """Stored as before the fix: position set, no key (plain run), or
    the patch's key (reprocess)."""
    author = "Al <a@x>"
    key = series_key("improve foo handling", author)
    _add(seeded_db, "c860@x", COVER, author, key=key, position=0)
    patch = _add(
        seeded_db, "p860@x", PATCH, author, parent="c860@x", key=key, position=1
    )
    _touch(seeded_db, patch)
    _add(seeded_db, "o860@x", REPLY, author, parent="p860@x", position=1)
    _add(seeded_db, "k860@x", REPLY, author, parent="p860@x", key=key, position=1)
    repost = _add(
        seeded_db, "rp860@x", REPLY, author, parent="p860@x", key=key, position=1
    )
    _touch(seeded_db, repost)

    plain = backfill_patch_series()

    assert plain.reply_patch == 1
    assert series_columns(seeded_db, "o860@x")["o860@x"] == (None,) * 4
    assert series_columns(seeded_db, "k860@x")["k860@x"][:3] == (key, "v2", 1)

    result = backfill_patch_series(reprocess=True)

    got = series_columns(seeded_db, "k860@x", "rp860@x")
    assert got["k860@x"] == (None,) * 4
    assert got["rp860@x"][:3] == (key, "v2", 1)
    assert result.reply_patch == 2


def test_a_repost_with_a_quilt_diff_is_not_a_reply(seeded_db, broker_active, tmp_path):
    """#869: a diff without git's header still makes it a patch."""
    quilt = "--- a/foo.c\n+++ b/foo.c\n@@ -1 +1 @@\n-a\n+b\n"
    cover = _msg("c869@x", COVER)
    patch = _with_body(_msg("p869@x", PATCH, in_reply_to="c869@x"), DIFF)
    repost = _with_body(_msg("rp869@x", REPLY, in_reply_to="p869@x"), quilt)
    _ingest(seeded_db, tmp_path, "0.git", [cover, patch, repost])

    assert series_columns(seeded_db, "rp869@x")["rp869@x"] == (KEY, "v2", 1, None)
    backfill_patch_series(reprocess=True)
    assert series_columns(seeded_db, "rp869@x")["rp869@x"] == (KEY, "v2", 1, None)
