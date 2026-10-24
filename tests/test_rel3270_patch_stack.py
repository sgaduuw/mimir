"""3.27.0 release review: the patch-series stack together (#837, #840,
#860, #862, #869, #870 parser, #870 cover typo).

One thread holds every shape at once, and ingest, `--reprocess`, a plain
run and the chain pass must give each row one answer; the second chain
pass plans nothing.
"""

from sqlalchemy import delete, select

from mimir.models import Article, ArticleFile
from mimir.patch_series import backfill_patch_series, series_key
from mimir.patch_series_chains import backfill_series_chains
from tests.test_chain_reply_patches import AUTHOR, _chain, _msg
from tests.test_ingest.test_series_rechain import T2026, _arrive

DAY = 86400
QUILT = (
    b"Index: linux/x.c\n===================================================\n"
    b"--- linux.orig/x.c\t2026-08-21 00:00:00\n+++ linux/x.c\t2026-08-21 00:00:01\n"
    b"@@ -1 +1 @@\n-a\n+b\n"
)
KEY = series_key("combo", AUTHOR)


def _thread():
    # Chain-reply posting whose cover says v2 over v3 patches (#840, #870).
    msgs = _chain("c", "v3", ["a", "b", "c"], cover_title="combo")
    msgs[0] = _msg("c@x", "[PATCH v2 0/3] combo", body=b"cover")
    # Same-sender no-diff reply repeating 2/3 (#860).
    msgs.append(_msg("c-r2@x", "[PATCH v3 2/3] b", "c-p2@x", body=b"Applied, thanks"))
    # Quilt-format re-post of 3/3 answering 3/3 (#869): stays a patch.
    msgs.append(_msg("c-q3@x", "[PATCH v3 3/3] c", "c-p3@x", body=QUILT))
    return msgs


def _late():
    # A v4 1/3 answering the v2-subject cover two days later (#862).
    return [_msg("c-late@x", "[PATCH v4 1/3] a", "c@x")]


def _snapshot(seeded_db):
    with seeded_db() as s:
        return s.execute(
            select(
                Article.message_id,
                Article.patch_series_key,
                Article.patch_series_version,
                Article.patch_series_position,
                Article.patch_series_head_id,
            )
            .where(Article.message_id.like("c%@x"))
            .order_by(Article.message_id)
        ).all()


def _planned():
    r = backfill_series_chains()
    return r.heads_planned, r.versions_planned


EXPECTED = {
    "c@x": (KEY, "v3", 0),
    "c-p1@x": (KEY, "v3", 1),
    "c-p2@x": (KEY, "v3", 2),
    "c-p3@x": (KEY, "v3", 3),
    "c-r2@x": (None, None, None),
    "c-q3@x": (KEY, "v3", 3),
    "c-late@x": (None, None, 1),
}


def _ingest(seeded_db, tmp_path):
    _arrive(seeded_db, tmp_path, "0.git", T2026, _thread())
    _arrive(seeded_db, tmp_path, "1.git", T2026 + 2 * DAY, _late())


def test_one_answer_across_ingest_reprocess_plain_and_chain(
    seeded_db, tmp_path, broker_active
):
    _ingest(seeded_db, tmp_path)
    ingested = _snapshot(seeded_db)
    assert {r[0]: tuple(r[1:4]) for r in ingested} == EXPECTED

    backfill_patch_series(reprocess=True)
    assert _snapshot(seeded_db) == ingested
    backfill_patch_series()
    assert _snapshot(seeded_db) == ingested
    assert _planned() == (0, 0)
    assert _planned() == (0, 0)
    assert _snapshot(seeded_db) == ingested

    backfill_patch_series(reprocess=True)
    assert _planned() == (0, 0)
    assert _snapshot(seeded_db) == ingested


def test_quilt_repost_stored_before_869(seeded_db, tmp_path, broker_active):
    """Documented limit, not a regression test: a quilt re-post stored
    before #869 has no article_files, and the required reprocess clears
    it as a reply until #874 or a backfill-article-files run."""
    _ingest(seeded_db, tmp_path)
    with seeded_db() as s:
        qid = s.scalar(select(Article.id).where(Article.message_id == "c-q3@x"))
        s.execute(delete(ArticleFile).where(ArticleFile.article_id == qid))
        s.commit()
    backfill_patch_series(reprocess=True)
    got = {r[0]: tuple(r[1:4]) for r in _snapshot(seeded_db)}
    assert got["c-q3@x"] == (None, None, None)


def test_cover_after_its_chain_with_replies(seeded_db, tmp_path, broker_active):
    """Patches, the no-diff reply and the quilt re-post land before their
    cover; a plain run links them, and every later pass agrees."""
    msgs = _thread()
    _arrive(seeded_db, tmp_path, "0.git", T2026, msgs[1:])
    _arrive(seeded_db, tmp_path, "1.git", T2026 + 60, msgs[:1])
    _arrive(seeded_db, tmp_path, "2.git", T2026 + 2 * DAY, _late())
    backfill_patch_series()
    linked = _snapshot(seeded_db)
    assert {r[0]: tuple(r[1:4]) for r in linked} == EXPECTED
    assert _planned() == (0, 0)
    backfill_patch_series(reprocess=True)
    assert _planned() == (0, 0)
    assert _snapshot(seeded_db) == linked
