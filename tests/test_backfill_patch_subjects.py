"""`backfill-article-files --patch-subjects` (#874): revisit only the
articles whose subject is an in-series patch, so the history a reply rule
reads `article_files` for is filled without reading the body of every
article in the archive."""

import re

from click.testing import CliRunner
from sqlalchemy import select, text

from mimir import backfill_runs, patches
from mimir.broker.protocol import BackfillArticleFilesRequest
from mimir.cli.backfill import backfill_article_files_command
from mimir.ingest import ingest_epoch
from mimir.models import Article, ArticleFile, Inbox
from mimir.patches import backfill_article_files
from tests.test_ingest._helpers import _build_pubinbox_repo, _clear_seed_articles

_GIT = "diff --git a/fs/g.c b/fs/g.c\n@@ -1 +1 @@\n-x\n+y\n"
_QUILT = "--- a/fs/q.c\n+++ b/fs/q.c\n@@ -1 +1 @@\n-x\n+y\n"

# (message id, subject, body), oldest first, so the walk meets them in
# reverse. Only the first two are in-series patches. The reply, the solo
# patch and the prose all yield rows or a verdict under a plain run.
_MAILS = [
    ("p1@x", "[PATCH 1/2] foo: a", _QUILT),
    ("p2@x", "[PATCH 2/2] foo: b", _QUILT),
    ("reply@x", "Re: [PATCH 1/2] foo: a", _GIT),
    ("solo@x", "[PATCH] foo: c", _GIT),
    ("prose@x", "hello", "no diff here\n"),
]
_IN_SERIES = {"p1@x", "p2@x"}


def _rfc5322(msgid: str, subject: str, body: str) -> bytes:
    return (
        f"Message-ID: <{msgid}>\r\n"
        "From: a@b.example\r\n"
        f"Subject: {subject}\r\n"
        "Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        f"\r\n{body}"
    ).encode()


def _corpus(seeded_db, tmp_path) -> None:
    """Ingest `_MAILS` as the only articles, then drop the rows ingest
    wrote: the world before #869 taught the extractor quilt diffs."""
    _clear_seed_articles(seeded_db)
    epoch_path = tmp_path / "0.git"
    _build_pubinbox_repo(epoch_path, [_rfc5322(*m) for m in _MAILS])
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        alpha.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, alpha, "0.git", epoch_path, workers=1)
        s.query(ArticleFile).delete()
        s.commit()
        assert s.query(Article).count() == len(_MAILS), "fixture did not ingest"


def _count_reads(monkeypatch) -> list[str]:
    """The message ids `_process_one` fetched a blob for."""
    seen: list[str] = []
    real = patches.read_first_readable

    def counting(session, candidates, prefer_inbox_id):
        seen.extend(a.message_id for a in candidates)
        return real(session, candidates, prefer_inbox_id)

    monkeypatch.setattr(patches, "read_first_readable", counting)
    return seen


def _filed(seeded_db) -> set[str]:
    with seeded_db() as s:
        return set(
            s.scalars(
                select(Article.message_id).join(
                    ArticleFile, ArticleFile.article_id == Article.id
                )
            )
        )


def test_an_in_series_patch_without_rows_gets_them(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    _corpus(seeded_db, tmp_path)
    assert _filed(seeded_db) == set(), "fixture left rows behind"
    reads = _count_reads(monkeypatch)

    result = backfill_article_files(patch_subjects=True)

    # The quilt patches, which only #869's extractor reads, got their rows.
    assert _filed(seeded_db) == _IN_SERIES
    assert (result.examined, result.indexed, result.skipped) == (5, 2, 3)
    assert result.no_diff == 0
    # Every article was walked, only the two patches had a blob fetched.
    assert sorted(reads) == sorted(_IN_SERIES)


def test_a_plain_run_still_reads_every_article(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    _corpus(seeded_db, tmp_path)
    reads = _count_reads(monkeypatch)

    result = backfill_article_files()

    assert sorted(reads) == sorted(m[0] for m in _MAILS)
    assert (result.examined, result.indexed, result.no_diff) == (5, 4, 1)
    assert result.skipped == 0
    assert _filed(seeded_db) == {"p1@x", "p2@x", "reply@x", "solo@x"}


def test_reprocess_with_patch_subjects_redoes_patches_only(
    seeded_db, tmp_path, broker_active, monkeypatch
):
    """The two options compose: `--reprocess` re-extracts, the subject
    filter limits it to in-series patches, and every other article keeps
    the rows it has."""
    _corpus(seeded_db, tmp_path)
    backfill_article_files()
    with seeded_db() as s:
        s.execute(
            text(
                "UPDATE article_files SET path = 'stale' WHERE article_id IN "
                "(SELECT id FROM articles WHERE message_id IN ('p1@x', 'solo@x'))"
            )
        )
        s.commit()
    reads = _count_reads(monkeypatch)

    result = backfill_article_files(patch_subjects=True, reprocess=True)

    assert (result.indexed, result.skipped) == (2, 3)
    assert sorted(reads) == sorted(_IN_SERIES)
    with seeded_db() as s:
        paths = dict(
            s.execute(
                select(Article.message_id, ArticleFile.path).join(
                    ArticleFile, ArticleFile.article_id == Article.id
                )
            ).all()
        )
    assert paths["p1@x"] == "fs/q.c"
    assert paths["solo@x"] == "stale"


def test_the_run_keys_differ_and_are_these_strings():
    assert backfill_runs.params_key() == '{"inbox_filter": null, "reprocess": false}'
    assert (
        backfill_runs.params_key(patch_subjects=True)
        == '{"inbox_filter": null, "patch_subjects": true, "reprocess": false}'
    )
    assert backfill_runs.params_key(reprocess=True, patch_subjects=True) == (
        '{"inbox_filter": null, "patch_subjects": true, "reprocess": true}'
    )


def _invoke(*args: str):
    return CliRunner().invoke(backfill_article_files_command, list(args))


def _keys(seeded_db) -> list[tuple[str, int | None]]:
    with seeded_db() as s:
        return [
            (key, cursor)
            for key, cursor in s.execute(
                text("SELECT params_key, cursor FROM backfill_runs ORDER BY id")
            )
        ]


def _resumed_at(output: str) -> int | None:
    m = re.search(r"resuming run started .+ at id (\d+)", output)
    return int(m.group(1)) if m else None


def test_the_option_and_a_plain_run_never_resume_each_other(
    seeded_db, tmp_path, broker_active
):
    _corpus(seeded_db, tmp_path)
    plain_key = '{"inbox_filter": null, "reprocess": false}'
    option_key = '{"inbox_filter": null, "patch_subjects": true, "reprocess": false}'

    first = _invoke("--patch-subjects", "--limit", "2")
    assert first.exit_code == 0, first.output
    assert _resumed_at(first.output) is None
    [(key, option_cursor)] = _keys(seeded_db)
    assert key == option_key and option_cursor is not None

    # A plain run starts its own run at the newest article.
    plain = _invoke("--limit", "1")
    assert plain.exit_code == 0, plain.output
    assert _resumed_at(plain.output) is None
    rows = _keys(seeded_db)
    assert [k for k, _ in rows] == [option_key, plain_key]
    plain_cursor = rows[1][1]
    assert plain_cursor is not None and plain_cursor != option_cursor

    # Each resumes below its own cursor.
    again = _invoke("--patch-subjects", "--limit", "1")
    assert _resumed_at(again.output) == option_cursor
    again = _invoke("--limit", "1")
    assert _resumed_at(again.output) == plain_cursor


def test_a_request_without_the_field_is_a_plain_run():
    """An older CLI sends no `patch_subjects`; it parses as plain."""
    req = BackfillArticleFilesRequest.model_validate({"limit": 3, "rpc_id": 1})
    assert req.patch_subjects is False


def _counts(seeded_db) -> dict[str, tuple[int | None, int | None]]:
    with seeded_db() as s:
        return {
            mid: (ins, dels)
            for mid, ins, dels in s.execute(
                select(
                    Article.message_id, ArticleFile.insertions, ArticleFile.deletions
                ).join(ArticleFile, ArticleFile.article_id == Article.id)
            )
        }


def test_reprocess_fills_line_counts_on_rows_stored_without_them(
    seeded_db, tmp_path, broker_active
):
    """#939: ingest writes each file's line counts, and rows stored before
    the counts existed read NULL. A run without `--reprocess` skips an
    article that has rows, so only `--patch-subjects --reprocess` fills
    them, and only for the in-series patches."""
    _clear_seed_articles(seeded_db)
    epoch_path = tmp_path / "0.git"
    _build_pubinbox_repo(epoch_path, [_rfc5322(*m) for m in _MAILS])
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        alpha.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, alpha, "0.git", epoch_path, workers=1)
    with_diff = {m[0] for m in _MAILS if m[0] != "prose@x"}
    assert _counts(seeded_db) == dict.fromkeys(with_diff, (1, 1))

    with seeded_db() as s:
        s.execute(text("UPDATE article_files SET insertions = NULL, deletions = NULL"))
        s.commit()
    backfill_article_files(patch_subjects=True)
    assert _counts(seeded_db) == dict.fromkeys(with_diff, (None, None))

    backfill_article_files(patch_subjects=True, reprocess=True)
    assert _counts(seeded_db) == {
        **dict.fromkeys(with_diff, (None, None)),
        **dict.fromkeys(_IN_SERIES, (1, 1)),
    }
