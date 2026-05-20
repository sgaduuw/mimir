"""Tests for mimir/subsystems.py: the article-level path-
matching primitives. `path_matches_glob` (directory /
exact-file / wildcard variants), `subsystems_for_article`
(F:/X: glob resolution), and `recent_patches_touching`
(reverse lookup from article to other patches touching
the same path)."""


from datetime import datetime, timedelta, timezone

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
    the whole point of "recent" patches touching X.

    Uses relative `now - N days` dates rather than fixed 2024
    timestamps so all three articles stay inside the 180-day
    `recent_patches_max_age_days` window (1.36.3) regardless of
    when the test runs.
    """
    with seeded_db() as s:
        from mimir.models import ArticleList
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        now = datetime.now(timezone.utc)
        for i, dt in enumerate([
            now - timedelta(days=60),  # oldest
            now - timedelta(days=10),  # newest
            now - timedelta(days=30),  # middle
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
    # date1 (10 days ago) > date2 (30 days ago) > date0 (60 days ago).
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
            date=datetime.now(timezone.utc) - timedelta(days=1),
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


def test_recent_patches_touching_respects_max_age_bound(seeded_db):
    """1.36.3: `recent_patches_max_age_days` (default 180) caps the
    sidebar to recent activity so the query plan walks a bounded
    date window instead of materialising every match for a
    "popular" file. An article older than the bound must not
    surface."""
    from mimir.config import settings
    from mimir.models import ArticleList

    with seeded_db() as s:
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        # One article inside the window (10 days ago), one outside
        # (default window + 30 days), both touching the same path.
        now = datetime.now(timezone.utc)
        recent_art = Article(
            message_id="recent@x", subject="x", author="a@x",
            date=now - timedelta(days=10),
            thread_parent=None, subject_normalized="x",
            canonical_inbox_id=inbox.id,
            lists=[ArticleList(inbox_id=inbox.id, epoch="0.git",
                               commit_sha="a" * 40)],
            files=[ArticleFile(path="fs/popular.c")],
        )
        ancient_art = Article(
            message_id="ancient@x", subject="x", author="a@x",
            date=now - timedelta(days=settings.recent_patches_max_age_days + 30),
            thread_parent=None, subject_normalized="x",
            canonical_inbox_id=inbox.id,
            lists=[ArticleList(inbox_id=inbox.id, epoch="0.git",
                               commit_sha="b" * 40)],
            files=[ArticleFile(path="fs/popular.c")],
        )
        s.add_all([recent_art, ancient_art])
        s.commit()
        out = recent_patches_touching(
            s, ["fs/popular.c"], exclude_article_id=-1,
        )
    # Only the recent one surfaces; the ancient one is below the
    # date floor.
    assert [r.message_id for r in out] == ["recent@x"]


def test_recent_patches_touching_uses_date_index_no_full_scan(seeded_db):
    """1.36.3 plan pin: the rewritten `recent_patches_touching`
    must drive on `ix_articles_date` over the bounded date window
    and test `EXISTS` per article via the
    `(article_id, path)` PK on `article_files`. Pre-1.36.3 the
    shape was `JOIN article_files ... WHERE path IN (...)
    GROUP BY article_id ORDER BY date DESC`, which materialised
    every match (millions of rows for popular files) before
    sorting; that ran 5+ minutes per request and starved
    gunicorn workers.

    Pin shape:
    - driver is `SEARCH a USING INDEX ix_articles_date` (any
      direction)
    - no `SCAN articles`
    - no `SCAN article_files`
    """
    from sqlalchemy import text

    with seeded_db() as s:
        # Seed at least one article so the planner has shape data.
        _add_patch_article(s, "plan@x", ["fs/x.c"])
        s.commit()

        # Build the query the way the helper does, but render the
        # compiled SQL via SQLAlchemy and pass it to EXPLAIN QUERY
        # PLAN. We can't easily intercept the live query, so we
        # mirror its shape with literal params.
        plan_rows = s.execute(
            text(
                """
                EXPLAIN QUERY PLAN
                SELECT a.id, a.message_id, a.subject, a.author,
                       a.date, a.canonical_inbox_id
                FROM articles a
                WHERE a.date >= :min_date
                  AND EXISTS (
                      SELECT 1 FROM article_files af
                      WHERE af.article_id = a.id
                        AND af.path IN ('fs/x.c')
                  )
                  AND a.id != -1
                ORDER BY a.date DESC
                LIMIT 5
                """
            ),
            {"min_date": (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()},
        ).all()
        plan = "\n".join(r[3] for r in plan_rows)

    assert "SCAN articles" not in plan, (
        f"full scan of articles in plan:\n{plan}"
    )
    assert "SCAN article_files" not in plan, (
        f"full scan of article_files in plan:\n{plan}"
    )
    # Driver should be the date index. Accept either ASC or DESC
    # direction; SQLite reports both as `USING INDEX ix_articles_date`.
    assert "ix_articles_date" in plan, (
        f"expected ix_articles_date as the driving index:\n{plan}"
    )


# `recent_articles_in_subsystem` integration, slice 1 of the
# per-subsystem dashboard. Filters by inbox + by the subsystem's
# include/exclude globs.
