"""Subsystem resolution against article-touched paths. Glob-matcher
unit tests + integration against the seeded test DB."""
from datetime import datetime, timezone

from sqlalchemy import select

from mimir.models import (
    Article,
    ArticleFile,
    Inbox,
    Subsystem,
    SubsystemMaintainer,
    SubsystemPath,
)
from mimir.subsystems import (
    path_matches_glob,
    recent_articles_in_subsystem,
    recent_patches_touching,
    subsystems_for_article,
)


# `path_matches_glob` unit tests — pure function, no DB.


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


# `subsystems_for_article` integration — uses the seeded DB and
# inserts MAINTAINERS-shaped fixtures inline.


def _add_subsystem(
    session, name, status, files, excludes=(), maintainers=(),
):
    """Insert a Subsystem + its paths + maintainers in one shot.
    Returns the inserted Subsystem (with .id assigned)."""
    sub = Subsystem(name=name, status=status)
    for f in files:
        sub.paths.append(SubsystemPath(glob=f, is_exclude=False))
    for x in excludes:
        sub.paths.append(SubsystemPath(glob=x, is_exclude=True))
    for role, mname, addr in maintainers:
        sub.maintainers.append(
            SubsystemMaintainer(role=role, name=mname, address=addr)
        )
    session.add(sub)
    session.flush()
    return sub


def _add_patch_article(session, msgid, paths, inbox_name="alpha"):
    """Insert a minimal Article + linked ArticleList + ArticleFile
    rows. Returns the Article id."""
    from mimir.models import ArticleList
    inbox = session.execute(
        select(Inbox).where(Inbox.name == inbox_name)
    ).scalar_one()
    art = Article(
        message_id=msgid,
        subject=f"patch {msgid}",
        author="a@example",
        date=datetime(2024, 6, 1, tzinfo=timezone.utc),
        thread_parent=None,
        subject_normalized=f"patch {msgid}",
        canonical_inbox_id=inbox.id,
        lists=[ArticleList(inbox_id=inbox.id, epoch="0.git",
                           commit_sha="f" * 40)],
        files=[ArticleFile(path=p) for p in paths],
    )
    session.add(art)
    session.flush()
    return art.id


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
    subsystems — the cheap-path early return matters at scale."""
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
    """Sidebar must surface the most recent activity first — that's
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
    whichever inbox the article was linked to first — so cross-
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


# `recent_articles_in_subsystem` integration — slice 1 of the
# per-subsystem dashboard. Filters by inbox + by the subsystem's
# include/exclude globs.


def test_recent_articles_in_subsystem_basic_match(seeded_db):
    """Articles whose paths match the subsystem's F: globs surface
    in the dashboard list; non-matching articles don't."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_patch_article(s, "in1@x", ["fs/bcachefs/super.c"])
        _add_patch_article(s, "in2@x", ["fs/bcachefs/io.c"])
        _add_patch_article(s, "out@x", ["fs/btrfs/extent.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = recent_articles_in_subsystem(s, alpha, sub)
    msgids = {p.message_id for p in out}
    assert msgids == {"in1@x", "in2@x"}


def test_recent_articles_in_subsystem_respects_exclude_globs(seeded_db):
    """A subsystem's X: globs veto articles whose only matched
    paths are excluded. The dashboard mirrors the patch-page
    header semantics."""
    with seeded_db() as s:
        sub = _add_subsystem(
            s, "BTRFS-MAIN", "Maintained",
            files=["fs/btrfs/"],
            excludes=["fs/btrfs/tests/"],
        )
        _add_patch_article(s, "main@x", ["fs/btrfs/extent.c"])
        _add_patch_article(s, "tests@x", ["fs/btrfs/tests/runner.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = recent_articles_in_subsystem(s, alpha, sub)
    assert {p.message_id for p in out} == {"main@x"}


def test_recent_articles_in_subsystem_keeps_article_with_one_in_scope_path(
    seeded_db,
):
    """An article touching both an included and an excluded path
    still belongs to the subsystem — the X: pass only vetoes
    articles whose paths are *all* excluded."""
    with seeded_db() as s:
        sub = _add_subsystem(
            s, "BTRFS-MAIN", "Maintained",
            files=["fs/btrfs/"],
            excludes=["fs/btrfs/tests/"],
        )
        _add_patch_article(s, "mixed@x", [
            "fs/btrfs/extent.c", "fs/btrfs/tests/runner.c",
        ])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = recent_articles_in_subsystem(s, alpha, sub)
    assert {p.message_id for p in out} == {"mixed@x"}


def test_recent_articles_in_subsystem_scoped_to_inbox(seeded_db):
    """Articles linked only to the other inbox don't appear."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_patch_article(s, "in-alpha@x", ["fs/bcachefs/super.c"],
                           inbox_name="alpha")
        _add_patch_article(s, "in-beta@x", ["fs/bcachefs/io.c"],
                           inbox_name="beta")
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        out_alpha = recent_articles_in_subsystem(s, alpha, sub)
        out_beta = recent_articles_in_subsystem(s, beta, sub)
    assert {p.message_id for p in out_alpha} == {"in-alpha@x"}
    assert {p.message_id for p in out_beta} == {"in-beta@x"}


def test_recent_articles_in_subsystem_orders_by_date_desc(seeded_db):
    """Newest articles first — operator wants "what's been happening
    in this subsystem lately" at the top."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        from mimir.models import ArticleList
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        for i, day in enumerate([10, 5, 15]):
            art = Article(
                message_id=f"d{i}@x",
                subject=f"patch {i}",
                author="a@x",
                date=datetime(2024, 6, day, tzinfo=timezone.utc),
                thread_parent=None, subject_normalized=f"patch {i}",
                canonical_inbox_id=alpha.id,
                lists=[ArticleList(inbox_id=alpha.id, epoch="0.git",
                                   commit_sha="f" * 40)],
                files=[ArticleFile(path="fs/bcachefs/super.c")],
            )
            s.add(art)
        s.commit()
        out = recent_articles_in_subsystem(s, alpha, sub)
    assert [p.date.day for p in out] == [15, 10, 5]


def test_recent_articles_in_subsystem_exact_path_glob(seeded_db):
    """A non-directory F: rule (e.g. `Documentation/foo.rst`) still
    matches articles touching that exact path. Wildcard globs are
    deliberately skipped in slice 1; this test pins the exact-path
    case which IS supported."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "DOCS", None, files=["Documentation/foo.rst"])
        _add_patch_article(s, "doc@x", ["Documentation/foo.rst"])
        _add_patch_article(s, "elsewhere@x", ["Documentation/bar.rst"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = recent_articles_in_subsystem(s, alpha, sub)
    assert {p.message_id for p in out} == {"doc@x"}


def test_recent_articles_in_subsystem_empty_when_no_matches(seeded_db):
    with seeded_db() as s:
        sub = _add_subsystem(s, "DORMANT", None, files=["drivers/dormant/"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = recent_articles_in_subsystem(s, alpha, sub)
    assert out == []


def test_recent_articles_in_subsystem_wildcard_globs_skipped_silently(
    seeded_db,
):
    """Slice 1 doesn't index wildcard globs. A subsystem with ONLY
    wildcard rules currently returns no articles even if articles
    would conceptually match — documented behaviour, not a crash."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "ARCH-CSTAR", None, files=["arch/*/cstar/"])
        _add_patch_article(s, "match@x", ["arch/x86/cstar/init.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = recent_articles_in_subsystem(s, alpha, sub)
    assert out == []
