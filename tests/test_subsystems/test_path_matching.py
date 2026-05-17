"""Tests for mimir/subsystems.py: the article-level path-
matching primitives. `path_matches_glob` (directory /
exact-file / wildcard variants), `subsystems_for_article`
(F:/X: glob resolution), and `recent_patches_touching`
(reverse lookup from article to other patches touching
the same path)."""


from datetime import datetime, timezone

from sqlalchemy import select

from mimir.models import (
    Article, ArticleFile, Inbox,
)
from mimir.subsystems import (
    path_matches_glob, recent_patches_touching,
    subsystems_for_article,
)

from tests.test_subsystems._helpers import _add_patch_article, _add_subsystem


def test_directory_glob_matches_paths_under_it():
    assert path_matches_glob("fs/bcachefs/super.c", "fs/bcachefs/")
    assert path_matches_glob("fs/bcachefs/util/", "fs/bcachefs/")
    # The bare directory path (no trailing slash on the article-
    # side) also matches: F: dir/ should claim mentions of dir.
    assert path_matches_glob("fs/bcachefs", "fs/bcachefs/")


def test_directory_glob_does_not_match_sibling_prefix():
    """`fs/bcachefs/` must not match `fs/bcachefs_foo/`. The
    trailing-slash discriminator is what makes prefix matches
    safe."""
    assert not path_matches_glob("fs/bcachefs_other/x.c", "fs/bcachefs/")
    assert not path_matches_glob("fs/btrfs/x.c", "fs/bcachefs/")


def test_exact_file_glob():
    assert path_matches_glob("Documentation/foo.rst", "Documentation/foo.rst")
    assert not path_matches_glob(
        "Documentation/foo.rst", "Documentation/bar.rst",
    )


def test_wildcard_glob_via_fnmatch():
    assert path_matches_glob("drivers/net/foo/file.c", "drivers/net/*")
    assert path_matches_glob("drivers/net/foo.c", "drivers/net/*.c")
    # `?` and `[]` also flow through fnmatch.
    assert path_matches_glob("a3.c", "a?.c")
    assert path_matches_glob("aN.c", "a[NM].c")


# `subsystems_for_article` integration, uses the seeded DB and
# inserts MAINTAINERS-shaped fixtures inline.


def test_subsystems_for_article_directory_match(seeded_db):
    with seeded_db() as s:
        _add_subsystem(
            s, "BCACHEFS", "Maintained",
            files=["fs/bcachefs/"],
            maintainers=[("M", "Kent Overstreet", "kent.overstreet@linux.dev")],
        )
        art_id = _add_patch_article(s, "p1@x", ["fs/bcachefs/super.c"])
        s.commit()

        hits = subsystems_for_article(s, art_id)
    assert len(hits) == 1
    assert hits[0].name == "BCACHEFS"
    assert hits[0].status == "Maintained"
    assert hits[0].maintainers == [
        ("M", "Kent Overstreet", "kent.overstreet@linux.dev"),
    ]


def test_subsystems_for_article_multi_subsystem(seeded_db):
    """A patch touching two unrelated subsystems lands both in the
    result, sorted by name for a stable header."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Maintained", files=["fs/bcachefs/"])
        _add_subsystem(s, "BTRFS", "Maintained", files=["fs/btrfs/"])
        art_id = _add_patch_article(s, "p2@x", [
            "fs/bcachefs/io.c", "fs/btrfs/extent_io.c",
        ])
        s.commit()
        hits = subsystems_for_article(s, art_id)
    assert [h.name for h in hits] == ["BCACHEFS", "BTRFS"]


def test_subsystems_for_article_exclude_vetoes_match(seeded_db):
    """`X:` (exclude) entries veto a `F:` match within the same
    subsystem. `fs/btrfs/tests/` should NOT pull in BTRFS-MAIN."""
    with seeded_db() as s:
        _add_subsystem(
            s, "BTRFS-MAIN", "Maintained",
            files=["fs/btrfs/"],
            excludes=["fs/btrfs/tests/"],
        )
        art_id = _add_patch_article(s, "p3@x", ["fs/btrfs/tests/runner.c"])
        s.commit()
        hits = subsystems_for_article(s, art_id)
    assert hits == []


def test_subsystems_for_article_exclude_only_vetoes_its_own_subsystem(
    seeded_db,
):
    """X: in subsystem A doesn't affect subsystem B. If A and B both
    cover the path, B's match survives even when A excludes it."""
    with seeded_db() as s:
        _add_subsystem(
            s, "BTRFS-MAIN", "Maintained",
            files=["fs/btrfs/"],
            excludes=["fs/btrfs/tests/"],
        )
        _add_subsystem(
            s, "BTRFS-TESTS", "Maintained",
            files=["fs/btrfs/tests/"],
        )
        art_id = _add_patch_article(s, "p4@x", ["fs/btrfs/tests/runner.c"])
        s.commit()
        hits = subsystems_for_article(s, art_id)
    assert [h.name for h in hits] == ["BTRFS-TESTS"]


def test_subsystems_for_article_no_paths_returns_empty(seeded_db):
    """A non-patch article (no ArticleFile rows) returns no
    subsystems, the cheap-path early return matters at scale."""
    with seeded_db() as s:
        _add_subsystem(s, "BTRFS", "Maintained", files=["fs/btrfs/"])
        art_id = _add_patch_article(s, "p5@x", [])
        s.commit()
        hits = subsystems_for_article(s, art_id)
    assert hits == []


# `recent_patches_touching` integration.


def test_recent_patches_touching_returns_others_sharing_path(seeded_db):
    """Two patches touch the same file → the sidebar surfaces the
    other one (not the current article)."""
    with seeded_db() as s:
        a = _add_patch_article(s, "p10@x", ["fs/bcachefs/super.c"])
        b = _add_patch_article(s, "p11@x", ["fs/bcachefs/super.c"])
        s.commit()
        out = recent_patches_touching(s, ["fs/bcachefs/super.c"],
                                      exclude_article_id=a)
    assert [r.article_id for r in out] == [b]


def test_recent_patches_touching_orders_by_date_desc(seeded_db):
    """Sidebar must surface the most recent activity first, that's
    the whole point of "recent" patches touching X."""
    with seeded_db() as s:
        from mimir.models import ArticleList
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        for i, dt in enumerate([
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 3, 1, tzinfo=timezone.utc),
            datetime(2024, 2, 1, tzinfo=timezone.utc),
        ]):
            art = Article(
                message_id=f"date{i}@x",
                subject="x", author="a@x", date=dt,
                thread_parent=None, subject_normalized="x",
                canonical_inbox_id=inbox.id,
                lists=[ArticleList(inbox_id=inbox.id, epoch="0.git",
                                   commit_sha="f" * 40)],
                files=[ArticleFile(path="fs/x.c")],
            )
            s.add(art)
        s.commit()
        out = recent_patches_touching(s, ["fs/x.c"], exclude_article_id=-1)
    # date1 (March) > date2 (Feb) > date0 (Jan)
    assert [r.message_id for r in out] == ["date1@x", "date2@x", "date0@x"]


def test_recent_patches_touching_respects_limit(seeded_db):
    with seeded_db() as s:
        for i in range(5):
            _add_patch_article(s, f"many{i}@x", ["fs/x.c"])
        s.commit()
        out = recent_patches_touching(s, ["fs/x.c"], exclude_article_id=-1, limit=2)
    assert len(out) == 2


def test_recent_patches_touching_resolves_canonical_inbox(seeded_db):
    """The sidebar's `inbox_name` is the canonical inbox, not just
    whichever inbox the article was linked to first, so cross-
    posts surface under the right URL."""
    with seeded_db() as s:
        from mimir.models import ArticleList
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        # Patch lives in both inboxes, canonical = beta.
        art = Article(
            message_id="canon@x", subject="x", author="a@x",
            date=datetime(2024, 6, 1, tzinfo=timezone.utc),
            thread_parent=None, subject_normalized="x",
            canonical_inbox_id=beta.id,
            lists=[
                ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="a" * 40),
                ArticleList(inbox_id=beta.id, epoch="0.git", commit_sha="b" * 40),
            ],
            files=[ArticleFile(path="fs/x.c")],
        )
        s.add(art)
        s.commit()
        out = recent_patches_touching(s, ["fs/x.c"], exclude_article_id=-1)
    assert len(out) == 1
    assert out[0].inbox_name == "beta"


# `recent_articles_in_subsystem` integration, slice 1 of the
# per-subsystem dashboard. Filters by inbox + by the subsystem's
# include/exclude globs.
