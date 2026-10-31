"""#939: a cover's series totals add the line counts of its patches'
files, "2 patches touching 3 files, +14/-3"."""

import re

from sqlalchemy import update

from mimir.models import Article, ArticleFile
from tests.test_routes.test_thread_posting import _seed, _visible

# (message id, path): (insertions, deletions). a.c is in both patches,
# so the totals add both of its rows. The v1 patch is another posting.
COUNTS = {
    ("p1@x", "drivers/foo/a.c"): (10, 2),
    ("p1@x", "drivers/foo/b.c"): (3, 0),
    ("p2@x", "drivers/foo/a.c"): (1, 1),
    ("p2@x", "fs/bar/c.c"): (0, 0),
    ("old@x", "net/zzz/old.c"): (99, 99),
}
# The thread header's Series card, as text: "2 patches, 3 files", then
# "+14 −3" in its own line (the minus is an entity in the HTML).
_TOTALS = re.compile(r"(\d+) patch(?:es)?, (\d+) files?(?:\s+\+(\d+) &minus;(\d+))?")


def _count(seeded, mid, path, counts):
    from mimir.extensions import SessionLocal

    ins, dels = counts
    with SessionLocal() as s:
        changed = s.execute(
            update(ArticleFile)
            .where(ArticleFile.article_id == seeded[mid][0], ArticleFile.path == path)
            .values(insertions=ins, deletions=dels)
        ).rowcount
        s.commit()
    assert changed == 1, (mid, path)


def _seed_counted(tmp_path):
    seeded = _seed(tmp_path)
    for (mid, path), counts in COUNTS.items():
        _count(seeded, mid, path, counts)
    return seeded


def _totals(client, url):
    return _TOTALS.findall(_visible(client.get(url).get_data(as_text=True)))


def test_cover_totals_add_the_patches_line_counts(client, tmp_path):
    seeded = _seed_counted(tmp_path)
    cover = seeded["cover@x"][1]
    assert _totals(client, cover + "/t") == [("2", "3", "14", "3")]
    md = client.get(cover + "/t.md").get_data(as_text=True)
    assert re.search(
        r"^- Series: 2 patches touching 3 files, \+14/-3$", md, re.MULTILINE
    ), md


def test_line_counts_stay_hidden_until_every_patch_file_has_one(client, tmp_path):
    """A partly backfilled posting would show a partial total as the
    whole, so the line counts wait for the last file. The rest of the
    line stays."""
    seeded = _seed_counted(tmp_path)
    _count(seeded, "p2@x", "fs/bar/c.c", (None, None))
    assert _totals(client, seeded["cover@x"][1] + "/t") == [("2", "3", "", "")]


def test_a_cover_s_own_rows_count_toward_neither_total(client, tmp_path):
    """A cover can carry an interdiff at column 0, which gives it file
    rows. Its lines would count twice, and `--patch-subjects` never
    re-reads a cover, so an uncounted row would hide the totals for
    good. Its files are the patches' files again."""
    from mimir.extensions import SessionLocal

    seeded = _seed_counted(tmp_path)
    cover_id = seeded["cover@x"][0]
    with SessionLocal() as s:
        s.add_all(
            [
                ArticleFile(
                    article_id=cover_id, path="net/zzz/x.c", insertions=5, deletions=5
                ),
                ArticleFile(article_id=cover_id, path="net/zzz/y.c"),
            ]
        )
        s.commit()
    html = client.get(seeded["cover@x"][1] + "/t").get_data(as_text=True)
    assert "zzz net" in _visible(html)  # precondition: the cover's rows are read
    assert _TOTALS.findall(_visible(html)) == [("2", "3", "14", "3")]


def test_a_count_rewritten_in_place_moves_the_cover_etag(client, tmp_path):
    """AGENTS.md: the validator carries every input the body depends on.
    A reprocess can rewrite a count at the same path. Caches are primed
    by a normal request, not cleared."""
    seeded = _seed_counted(tmp_path)
    url = seeded["cover@x"][1] + "/t"
    before = client.get(url)
    assert _TOTALS.findall(_visible(before.get_data(as_text=True)))[0][2] == "14"

    _count(seeded, "p1@x", "drivers/foo/a.c", (20, 2))

    after = client.get(url, headers={"If-None-Match": before.headers["ETag"]})
    assert after.status_code == 200, "served 304 after a count changed"
    assert _TOTALS.findall(_visible(after.get_data(as_text=True))) == [
        ("2", "3", "24", "3")
    ]


def test_a_count_change_leaves_the_patch_s_own_etag(client, tmp_path):
    """A patch page shows no totals, so the backfill writing its counts
    must not send every patch page back to crawlers. A patch in a
    cover's thread has only its message page (its `/t` redirects to
    the cover's); the v1 patch roots its own thread."""
    seeded = _seed_counted(tmp_path)
    urls = (seeded["p1@x"][1], seeded["old@x"][1], seeded["old@x"][1] + "/t")
    before = {u: client.get(u).headers["ETag"] for u in urls}

    _count(seeded, "p1@x", "drivers/foo/a.c", (20, 2))
    _count(seeded, "old@x", "net/zzz/old.c", (1, 1))

    for u in urls:
        assert client.get(u, headers={"If-None-Match": before[u]}).status_code == 304


def test_a_resent_patch_counts_once_in_the_line_totals(client, tmp_path):
    from mimir.extensions import SessionLocal

    seeded = _seed_counted(tmp_path)
    p1_id = seeded["p1@x"][0]
    with SessionLocal() as s:
        p1 = s.get(Article, p1_id)
        # The same patch sent again later: same series, version and
        # position, same files and counts, a higher id.
        resend = Article(
            message_id="p1-resend@x",
            subject=p1.subject,
            author=p1.author,
            date=p1.date,
            patch_series_key=p1.patch_series_key,
            patch_series_head_id=p1.patch_series_head_id,
            patch_series_version=p1.patch_series_version,
            patch_series_position=p1.patch_series_position,
        )
        s.add(resend)
        s.flush()
        s.add_all(
            [
                ArticleFile(
                    article_id=resend.id,
                    path="drivers/foo/a.c",
                    insertions=10,
                    deletions=2,
                ),
                ArticleFile(
                    article_id=resend.id,
                    path="drivers/foo/b.c",
                    insertions=3,
                    deletions=0,
                ),
            ]
        )
        s.commit()

    html = client.get(seeded["cover@x"][1] + "/t").get_data(as_text=True)
    # Two patches, three files, and p1's lines once: +14/-3, as before the resend.
    assert _TOTALS.findall(_visible(html)) == [("2", "3", "14", "3")], re.findall(
        r"Series:.*", _visible(html)
    )
