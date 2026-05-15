"""Subsystem resolution against article-touched paths. Glob-matcher
unit tests + integration against the seeded test DB."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from mimir.models import (
    Article,
    ArticleFile,
    ArticleTrailer,
    Inbox,
    Subsystem,
    SubsystemMaintainer,
    SubsystemPath,
)
from mimir.subsystems import (
    path_matches_glob,
    recent_patches_touching,
    subsystems_for_article,
)
from mimir.subsystems_dashboard import (
    active_reviewers_in_subsystem,
    active_threads_in_subsystem,
    articles_reviewed_by,
    daily_volume_in_subsystem,
    most_active_subsystems_global,
    most_active_subsystems_in_inbox,
    recent_articles_in_subsystem,
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


def test_recent_articles_in_subsystem_is_cached(seeded_db):
    """The helper is the dominant cold-load cost of the per-subsystem
    dashboard (joins article_files × article_lists × articles with one
    OR clause per F: glob, then a Python X: pass). A repeated call
    must short-circuit through the cache instead of re-running the
    join; a `force=True` call must bypass."""
    from mimir import cache
    from mimir.cache import _ns
    from mimir.models import CacheEntry
    from sqlalchemy import delete as sql_delete

    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_patch_article(s, "cache-hit@x", ["fs/bcachefs/super.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()

        # Clean any prior cache row for this (inbox, subsystem, limit).
        nskey = _ns(f"recent_articles_in_subsystem:alpha:{sub.id}:20")
        s.execute(sql_delete(CacheEntry).where(CacheEntry.key == nskey))
        s.commit()

        # First call computes + caches.
        first = recent_articles_in_subsystem(s, alpha, sub)
        assert {p.message_id for p in first} == {"cache-hit@x"}

        # Cache row landed.
        assert cache.get(f"recent_articles_in_subsystem:alpha:{sub.id}:20") is not None

        # Add a NEW matching article that would surface if the helper
        # recomputed. Without force the cache hides it.
        _add_patch_article(s, "added-after@x", ["fs/bcachefs/io.c"])
        s.commit()
        second = recent_articles_in_subsystem(s, alpha, sub)
        assert {p.message_id for p in second} == {"cache-hit@x"}, (
            f"expected cache hit returning stale row; got recompute: {second}"
        )

        # force=True bypasses and picks up the new article.
        third = recent_articles_in_subsystem(s, alpha, sub, force=True)
        assert {p.message_id for p in third} == {"cache-hit@x", "added-after@x"}


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


# `active_threads_in_subsystem` integration — slice 2 of the
# per-subsystem dashboard. Same decay-weighted scoring as the
# landing-page `active_threads`, but constrained to messages
# touching the subsystem's paths.


def _add_recent_thread_root(
    session, msgid, paths, subject="recent root", inbox_name="alpha",
):
    """Insert a recent (today-ish) article with ArticleFile rows so
    the active-threads CTE picks it up as a seed."""
    from mimir.models import ArticleList
    inbox = session.execute(
        select(Inbox).where(Inbox.name == inbox_name)
    ).scalar_one()
    art = Article(
        message_id=msgid,
        subject=subject,
        author="a@example",
        date=datetime.now(timezone.utc) - timedelta(hours=1),
        thread_parent=None,
        subject_normalized=subject,
        canonical_inbox_id=inbox.id,
        lists=[ArticleList(inbox_id=inbox.id, epoch="0.git",
                           commit_sha="f" * 40)],
        files=[ArticleFile(path=p) for p in paths],
    )
    session.add(art)
    session.flush()
    return art


def test_active_threads_in_subsystem_returns_threads_with_matching_path(
    seeded_db,
):
    """A thread with a recent message touching the subsystem's
    paths surfaces in the dashboard's active-threads list. A
    thread with a recent message touching unrelated paths does
    not."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(s, "bch@x", ["fs/bcachefs/super.c"],
                                subject="bcachefs work")
        _add_recent_thread_root(s, "other@x", ["fs/btrfs/extent.c"],
                                subject="btrfs work")
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = active_threads_in_subsystem(s, alpha, sub, force=True)
    msgids = {t.message_id for t in out}
    assert "bch@x" in msgids
    assert "other@x" not in msgids


def test_active_threads_in_subsystem_respects_excludes(seeded_db):
    """X: globs veto threads whose seed message's paths are all
    excluded — same per-article-keep-if-one-in-scope rule as the
    recent-patches surface."""
    with seeded_db() as s:
        sub = _add_subsystem(
            s, "BTRFS-MAIN", "Maintained",
            files=["fs/btrfs/"], excludes=["fs/btrfs/tests/"],
        )
        _add_recent_thread_root(s, "main@x", ["fs/btrfs/extent.c"],
                                subject="main work")
        _add_recent_thread_root(s, "tests@x", ["fs/btrfs/tests/runner.c"],
                                subject="tests work")
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = active_threads_in_subsystem(s, alpha, sub, force=True)
    msgids = {t.message_id for t in out}
    assert msgids == {"main@x"}


def test_active_threads_in_subsystem_scoped_to_inbox(seeded_db):
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(s, "a-side@x", ["fs/bcachefs/a.c"],
                                inbox_name="alpha")
        _add_recent_thread_root(s, "b-side@x", ["fs/bcachefs/b.c"],
                                inbox_name="beta")
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        out_alpha = active_threads_in_subsystem(s, alpha, sub, force=True)
        out_beta = active_threads_in_subsystem(s, beta, sub, force=True)
    assert {t.message_id for t in out_alpha} == {"a-side@x"}
    assert {t.message_id for t in out_beta} == {"b-side@x"}


def test_active_threads_in_subsystem_empty_when_wildcard_only(seeded_db):
    """A subsystem with only wildcard F: rules has no supported
    globs in slice 2 and returns no active threads — same
    documented behaviour as `recent_articles_in_subsystem`."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "ARCH-CSTAR", None, files=["arch/*/cstar/"])
        _add_recent_thread_root(s, "match@x", ["arch/x86/cstar/init.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = active_threads_in_subsystem(s, alpha, sub, force=True)
    assert out == []


# `daily_volume_in_subsystem` integration.


def test_daily_volume_in_subsystem_counts_matching_articles(seeded_db):
    """An article touching a subsystem path increments that day's
    bar. An article touching an unrelated path does not."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(s, "in@x", ["fs/bcachefs/super.c"])
        _add_recent_thread_root(s, "out@x", ["fs/btrfs/extent.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        spark = daily_volume_in_subsystem(s, alpha, sub, days=7, force=True)
    # Today's bar should reflect the one in-scope article.
    today_count = sum(c for _, c in spark.days)
    assert today_count == 1


def test_daily_volume_in_subsystem_zero_fills_when_empty(seeded_db):
    """A dormant subsystem still returns a fully zero-filled
    `days` series so the sparkline renders cleanly."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "DORMANT", None, files=["drivers/dormant/"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        spark = daily_volume_in_subsystem(s, alpha, sub, days=14, force=True)
    assert len(spark.days) == 14
    assert all(c == 0 for _, c in spark.days)


def test_daily_volume_in_subsystem_returns_zero_series_for_wildcard_only(
    seeded_db,
):
    """A subsystem with only wildcard F: rules has no supported
    globs in slice 2 — the sparkline still renders, all zeros."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "ARCH-CSTAR", None, files=["arch/*/cstar/"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        spark = daily_volume_in_subsystem(s, alpha, sub, days=10, force=True)
    assert len(spark.days) == 10
    assert all(c == 0 for _, c in spark.days)


# `active_reviewers_in_subsystem` integration — slice 2 of #97.
# The extractor itself is exercised in tests/test_trailers.py; here
# we pin the JOIN through article_files (subsystem path filter) +
# the per-reviewer aggregation contract.


def _add_recent_patch_with_trailers(
    session, msgid, paths, trailers, inbox_name="alpha", days_ago=0,
):
    """Insert a recent article with ArticleFile rows + ArticleTrailer
    rows. `trailers` is a list of (role, name, address) tuples."""
    from mimir.models import ArticleList
    inbox = session.execute(
        select(Inbox).where(Inbox.name == inbox_name)
    ).scalar_one()
    art = Article(
        message_id=msgid,
        subject=f"patch {msgid}",
        author="a@example",
        date=datetime.now(timezone.utc) - timedelta(days=days_ago, hours=1),
        thread_parent=None,
        subject_normalized=f"patch {msgid}",
        canonical_inbox_id=inbox.id,
        lists=[ArticleList(inbox_id=inbox.id, epoch="0.git",
                           commit_sha="f" * 40)],
        files=[ArticleFile(path=p) for p in paths],
        trailers=[
            ArticleTrailer(
                role=role, name=name,
                address=addr, address_normalized=addr.lower(),
            )
            for role, name, addr in trailers
        ],
    )
    session.add(art)
    session.flush()
    return art


def test_active_reviewers_groups_attestations_by_address(seeded_db):
    """One reviewer who appears on multiple patches collapses to a
    single ReviewerStat with summed counts."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_patch_with_trailers(
            s, "p1@x", ["fs/bcachefs/super.c"],
            [("Reviewed-by", "Alice", "alice@kernel.org")],
        )
        _add_recent_patch_with_trailers(
            s, "p2@x", ["fs/bcachefs/io.c"],
            [
                ("Reviewed-by", "Alice", "alice@kernel.org"),
                ("Acked-by", "Bob", "bob@kernel.org"),
            ],
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = active_reviewers_in_subsystem(s, alpha, sub, force=True)

    by_addr = {r.address_normalized: r for r in out}
    assert set(by_addr) == {"alice@kernel.org", "bob@kernel.org"}
    assert by_addr["alice@kernel.org"].role_counts == {"Reviewed-by": 2}
    assert by_addr["alice@kernel.org"].total == 2
    assert by_addr["bob@kernel.org"].role_counts == {"Acked-by": 1}


def test_active_reviewers_orders_by_total_then_recency(seeded_db):
    """Primary sort: total desc. Tiebreak: last_seen desc so equally-
    active reviewers surface freshest-first."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        # carol: 2 attestations, oldest activity 2 days ago
        _add_recent_patch_with_trailers(
            s, "old1@x", ["fs/bcachefs/a.c"],
            [("Reviewed-by", "Carol", "carol@kernel.org")],
            days_ago=2,
        )
        _add_recent_patch_with_trailers(
            s, "old2@x", ["fs/bcachefs/b.c"],
            [("Reviewed-by", "Carol", "carol@kernel.org")],
            days_ago=2,
        )
        # dave: 2 attestations, latest activity today
        _add_recent_patch_with_trailers(
            s, "new1@x", ["fs/bcachefs/c.c"],
            [("Reviewed-by", "Dave", "dave@kernel.org")],
            days_ago=0,
        )
        _add_recent_patch_with_trailers(
            s, "new2@x", ["fs/bcachefs/d.c"],
            [("Reviewed-by", "Dave", "dave@kernel.org")],
            days_ago=2,
        )
        # erin: 1 attestation, today — should rank below the two-counters
        _add_recent_patch_with_trailers(
            s, "single@x", ["fs/bcachefs/e.c"],
            [("Reviewed-by", "Erin", "erin@kernel.org")],
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = active_reviewers_in_subsystem(s, alpha, sub, force=True)

    addrs = [r.address_normalized for r in out]
    # dave (2 today) > carol (2 two-days-ago) > erin (1 today).
    assert addrs == ["dave@kernel.org", "carol@kernel.org", "erin@kernel.org"]


def test_active_reviewers_respects_subsystem_excludes(seeded_db):
    """X: globs veto trailers on patches whose paths are all
    excluded — same path-filter posture as the other subsystem
    helpers."""
    with seeded_db() as s:
        sub = _add_subsystem(
            s, "BTRFS-MAIN", "Maintained",
            files=["fs/btrfs/"], excludes=["fs/btrfs/tests/"],
        )
        _add_recent_patch_with_trailers(
            s, "main@x", ["fs/btrfs/extent.c"],
            [("Reviewed-by", "Alice", "alice@kernel.org")],
        )
        _add_recent_patch_with_trailers(
            s, "tests@x", ["fs/btrfs/tests/runner.c"],
            [("Reviewed-by", "Bob", "bob@kernel.org")],
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = active_reviewers_in_subsystem(s, alpha, sub, force=True)

    addrs = {r.address_normalized for r in out}
    assert addrs == {"alice@kernel.org"}


def test_active_reviewers_scoped_to_inbox(seeded_db):
    """A reviewer active on the same paths in a different inbox
    doesn't bleed into this inbox's surface."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_patch_with_trailers(
            s, "a@x", ["fs/bcachefs/x.c"],
            [("Reviewed-by", "Alpha-only", "ao@kernel.org")],
            inbox_name="alpha",
        )
        _add_recent_patch_with_trailers(
            s, "b@x", ["fs/bcachefs/y.c"],
            [("Reviewed-by", "Beta-only", "bo@kernel.org")],
            inbox_name="beta",
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        out_alpha = active_reviewers_in_subsystem(s, alpha, sub, force=True)
        out_beta = active_reviewers_in_subsystem(s, beta, sub, force=True)

    assert {r.address_normalized for r in out_alpha} == {"ao@kernel.org"}
    assert {r.address_normalized for r in out_beta} == {"bo@kernel.org"}


def test_active_reviewers_empty_when_no_supported_globs(seeded_db):
    """Wildcard-only F: rules → no supported globs → empty list,
    matching the documented contract of the other path-filtered
    helpers."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "ARCH-CSTAR", None, files=["arch/*/cstar/"])
        _add_recent_patch_with_trailers(
            s, "wild@x", ["arch/x86/cstar/init.c"],
            [("Reviewed-by", "Anyone", "any@kernel.org")],
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = active_reviewers_in_subsystem(s, alpha, sub, force=True)
    assert out == []


# `articles_reviewed_by` integration — slice 3 of #97. Powers the
# per-reviewer `/<inbox>/reviewer/<address>` page.


def test_articles_reviewed_by_returns_one_entry_per_attestation(seeded_db):
    """Same person under two roles on one patch (Reported-by +
    Tested-by) shows as two ReviewEntry rows. Accurate to the source
    trailer block; the per-reviewer page exists to surface every
    attestation this person made."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_patch_with_trailers(
            s, "p1@x", ["fs/bcachefs/super.c"],
            [
                ("Reported-by", "Alice", "alice@kernel.org"),
                ("Tested-by", "Alice", "alice@kernel.org"),
            ],
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = articles_reviewed_by(s, alpha, "alice@kernel.org", force=True)
    roles = sorted(e.role for e in out)
    assert roles == ["Reported-by", "Tested-by"]
    assert {e.message_id for e in out} == {"p1@x"}
    # Suppress unused fixture warning for `sub` (the helper resolves
    # by address, not by subsystem — the patch happens to be in one
    # for fixture-construction convenience).
    assert sub.id is not None


def test_articles_reviewed_by_matches_address_case_insensitively(
    seeded_db,
):
    """Address comes through `address_normalized` (lowercased at
    ingest); the helper takes the lowercased URL value. Verify that
    a mixed-case stored address still matches when queried with the
    normalized form."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        # Address stored with original-casing as it landed in the
        # trailer; address_normalized is lowercased.
        from mimir.models import ArticleList, ArticleTrailer
        inbox = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        art = Article(
            message_id="case@x",
            subject="case-test",
            author="a@example",
            date=datetime.now(timezone.utc),
            thread_parent=None,
            subject_normalized="case-test",
            canonical_inbox_id=inbox.id,
            lists=[ArticleList(inbox_id=inbox.id, epoch="0.git",
                               commit_sha="f" * 40)],
            files=[ArticleFile(path="fs/bcachefs/x.c")],
            trailers=[ArticleTrailer(
                role="Reviewed-by", name="Mixed",
                address="Mixed@KerneL.OrG",
                address_normalized="mixed@kernel.org",
            )],
        )
        s.add(art)
        s.commit()
        out = articles_reviewed_by(s, inbox, "mixed@kernel.org", force=True)
    assert {e.message_id for e in out} == {"case@x"}


def test_articles_reviewed_by_orders_by_date_desc(seeded_db):
    """Newest attestations surface first — the page is a chronological
    listing, freshest at the top."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_patch_with_trailers(
            s, "old@x", ["fs/bcachefs/a.c"],
            [("Reviewed-by", "A", "a@kernel.org")], days_ago=10,
        )
        _add_recent_patch_with_trailers(
            s, "new@x", ["fs/bcachefs/b.c"],
            [("Reviewed-by", "A", "a@kernel.org")], days_ago=0,
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = articles_reviewed_by(s, alpha, "a@kernel.org", force=True)
    assert [e.message_id for e in out] == ["new@x", "old@x"]


def test_articles_reviewed_by_scoped_to_inbox(seeded_db):
    """Attestations on patches in `beta` don't bleed into `alpha`'s
    reviewer page — the URL is inbox-scoped and the helper joins
    through `article_lists`."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_patch_with_trailers(
            s, "a-side@x", ["fs/bcachefs/x.c"],
            [("Reviewed-by", "A", "a@kernel.org")],
            inbox_name="alpha",
        )
        _add_recent_patch_with_trailers(
            s, "b-side@x", ["fs/bcachefs/y.c"],
            [("Reviewed-by", "A", "a@kernel.org")],
            inbox_name="beta",
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        out_alpha = articles_reviewed_by(s, alpha, "a@kernel.org", force=True)
        out_beta = articles_reviewed_by(s, beta, "a@kernel.org", force=True)
    assert {e.message_id for e in out_alpha} == {"a-side@x"}
    assert {e.message_id for e in out_beta} == {"b-side@x"}


def test_articles_reviewed_by_respects_limit(seeded_db):
    """The cap prevents per-reviewer pages from growing unbounded."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        for i in range(5):
            _add_recent_patch_with_trailers(
                s, f"p{i}@x", ["fs/bcachefs/x.c"],
                [("Reviewed-by", "A", "a@kernel.org")],
                days_ago=i,
            )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = articles_reviewed_by(s, alpha, "a@kernel.org", limit=3, force=True)
    assert len(out) == 3
    # Newest 3 (days_ago = 0, 1, 2).
    assert [e.message_id for e in out] == ["p0@x", "p1@x", "p2@x"]


def test_articles_reviewed_by_resolves_canonical_inbox(seeded_db):
    """Cross-posted articles get the canonical inbox name on each
    ReviewEntry, so the message URL constructed in the template
    points at the right inbox even when the URL is reached via a
    different inbox's reviewer page."""
    from mimir.models import ArticleList, ArticleTrailer
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        # Cross-posted: belongs to both alpha and beta; canonical = beta.
        art = Article(
            message_id="xpost@x",
            subject="cross-post",
            author="a@example",
            date=datetime.now(timezone.utc),
            thread_parent=None,
            subject_normalized="cross-post",
            canonical_inbox_id=beta.id,
            lists=[
                ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="a" * 40),
                ArticleList(inbox_id=beta.id, epoch="0.git", commit_sha="b" * 40),
            ],
            files=[ArticleFile(path="fs/bcachefs/x.c")],
            trailers=[ArticleTrailer(
                role="Reviewed-by", name="A", address="a@kernel.org",
                address_normalized="a@kernel.org",
            )],
        )
        s.add(art)
        s.commit()
        # Query the per-reviewer page from alpha; entry should still
        # carry beta as the inbox_name (where the canonical URL lives).
        out = articles_reviewed_by(s, alpha, "a@kernel.org", force=True)
    assert len(out) == 1
    assert out[0].inbox_name == "beta"


def test_active_reviewers_keeps_latest_display_name_per_address(seeded_db):
    """When the same address appears under different display names
    across patches (people change `Name` in their git config), the
    most-recent attestation's name wins. Address is the stable
    identity."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_patch_with_trailers(
            s, "old@x", ["fs/bcachefs/a.c"],
            [("Reviewed-by", "Old Name", "person@kernel.org")],
            days_ago=2,
        )
        _add_recent_patch_with_trailers(
            s, "new@x", ["fs/bcachefs/b.c"],
            [("Reviewed-by", "New Name", "person@kernel.org")],
            days_ago=0,
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = active_reviewers_in_subsystem(s, alpha, sub, force=True)
    assert len(out) == 1
    assert out[0].name == "New Name"
    assert out[0].total == 2


# `most_active_subsystems_in_inbox` + `most_active_subsystems_global`
# integration tests. Powers the subsystem discoverability surfaces
# on the inbox dashboard and the front page.


def test_most_active_subsystems_in_inbox_counts_and_sorts(seeded_db):
    """Two subsystems with different recent message counts surface
    in descending order. Subsystem with no recent activity is
    excluded entirely."""
    with seeded_db() as s:
        hot = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        warm = _add_subsystem(s, "BTRFS-MAIN", "Maintained", files=["fs/btrfs/"])
        _add_subsystem(s, "DORMANT", None, files=["drivers/dormant/"])
        # 3 hot, 1 warm, 0 dormant.
        _add_recent_thread_root(s, "h1@x", ["fs/bcachefs/a.c"])
        _add_recent_thread_root(s, "h2@x", ["fs/bcachefs/b.c"])
        _add_recent_thread_root(s, "h3@x", ["fs/bcachefs/c.c"])
        _add_recent_thread_root(s, "w1@x", ["fs/btrfs/extent.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = most_active_subsystems_in_inbox(
            s, alpha, days=7, limit=10, force=True,
        )
    names = [a.name for a in out]
    counts = [a.message_count for a in out]
    assert names == ["BCACHEFS", "BTRFS-MAIN"]
    assert counts == [3, 1]
    # `DORMANT` doesn't appear at all (no in-window articles).
    assert hot.id in {a.id for a in out}
    assert warm.id in {a.id for a in out}


def test_most_active_subsystems_in_inbox_inbox_scoped(seeded_db):
    """A subsystem's count on `alpha` doesn't bleed into `beta`'s
    list."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(s, "alpha-1@x", ["fs/bcachefs/a.c"],
                                inbox_name="alpha")
        _add_recent_thread_root(s, "alpha-2@x", ["fs/bcachefs/b.c"],
                                inbox_name="alpha")
        _add_recent_thread_root(s, "beta-1@x", ["fs/bcachefs/c.c"],
                                inbox_name="beta")
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        out_a = most_active_subsystems_in_inbox(s, alpha, force=True)
        out_b = most_active_subsystems_in_inbox(s, beta, force=True)
    assert out_a[0].message_count == 2
    assert out_a[0].inbox_name == "alpha"
    assert out_b[0].message_count == 1
    assert out_b[0].inbox_name == "beta"


def test_most_active_subsystems_in_inbox_empty_when_no_supported_globs(
    seeded_db,
):
    """Wildcard-only F: rules → no supported globs → not included.
    Matches the documented contract of the other path-filtered
    helpers."""
    with seeded_db() as s:
        _add_subsystem(s, "ARCH-CSTAR", None, files=["arch/*/cstar/"])
        _add_recent_thread_root(s, "wild@x", ["arch/x86/cstar/init.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = most_active_subsystems_in_inbox(s, alpha, force=True)
    assert out == []


def test_most_active_subsystems_global_aggregates_across_inboxes(
    seeded_db,
):
    """A subsystem active in both inboxes shows once with a total
    count summed across them, attributed to the busiest inbox."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        # alpha: 3 messages
        _add_recent_thread_root(s, "a1@x", ["fs/bcachefs/a.c"], inbox_name="alpha")
        _add_recent_thread_root(s, "a2@x", ["fs/bcachefs/b.c"], inbox_name="alpha")
        _add_recent_thread_root(s, "a3@x", ["fs/bcachefs/c.c"], inbox_name="alpha")
        # beta: 1 message
        _add_recent_thread_root(s, "b1@x", ["fs/bcachefs/d.c"], inbox_name="beta")
        s.commit()
        out = most_active_subsystems_global(s, days=7, limit=10, force=True)
    assert len(out) == 1
    assert out[0].name == "BCACHEFS"
    # Total = 3 + 1 = 4. Attribution = alpha (3 > 1).
    assert out[0].message_count == 4
    assert out[0].inbox_name == "alpha"


def test_most_active_subsystems_global_force_propagates_to_inner(
    seeded_db, monkeypatch,
):
    """The outer `most_active_subsystems_global` wraps its compute
    in `cache.get_or_compute`, and the inner per-inbox helper has
    its own cache. Without `force=force` plumbed through, a
    `warm-cache --force` (or any caller passing `force=True`)
    recomputes the global aggregator from stale per-inbox rows.
    Audit (2026-05-15) flagged it: outer bypasses, inner silently
    doesn't.

    The test patches the inner helper to record the `force` kwarg
    every call gets, then drives the public surface with
    `force=True` and asserts every inner invocation saw `True`."""
    from mimir import subsystems_dashboard as subs_mod

    seen_force: list[bool] = []
    real_inner = subs_mod._most_active_subsystems_in_inbox_full

    def _spy(session, inbox, *, days=7, force=False):
        seen_force.append(force)
        return real_inner(session, inbox, days=days, force=force)

    monkeypatch.setattr(
        subs_mod, "_most_active_subsystems_in_inbox_full", _spy,
    )

    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(s, "force-a@x", ["fs/bcachefs/a.c"], inbox_name="alpha")
        s.commit()
        most_active_subsystems_global(s, days=7, limit=10, force=True)

    assert seen_force, "inner helper was never called, test setup broke"
    assert all(seen_force), (
        f"force=True must propagate through to the per-inbox helper; "
        f"got {seen_force!r}"
    )


def test_most_active_subsystems_global_alphabetical_tiebreak_on_inbox(
    seeded_db,
):
    """Equal per-inbox counts → attribute to the alphabetically-
    earlier inbox name. Deterministic ordering matters for stable
    URL generation."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(s, "a-tie@x", ["fs/bcachefs/a.c"], inbox_name="alpha")
        _add_recent_thread_root(s, "b-tie@x", ["fs/bcachefs/b.c"], inbox_name="beta")
        s.commit()
        out = most_active_subsystems_global(s, days=7, limit=10, force=True)
    assert len(out) == 1
    # alpha < beta alphabetically, equal per-inbox count of 1.
    assert out[0].inbox_name == "alpha"


def test_most_active_subsystems_carries_top_maintainer_and_status(
    seeded_db,
):
    """SubsystemActivity rows pick up the first M: maintainer's
    name + the subsystem's status. The R: rows don't contribute
    to the "maintained by" decoration (that framing is M:-only)."""
    with seeded_db() as s:
        _add_subsystem(
            s, "BCACHEFS", "Supported",
            files=["fs/bcachefs/"],
            maintainers=[
                ("M", "Kent Overstreet", "kent@kernel.org"),
                ("R", "Brian Foster", "bfoster@redhat.com"),
            ],
        )
        _add_recent_thread_root(s, "bch@x", ["fs/bcachefs/super.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = most_active_subsystems_in_inbox(s, alpha, force=True)
    assert len(out) == 1
    row = out[0]
    assert row.maintainer_name == "Kent Overstreet"
    assert row.multiple_maintainers is False  # only one M: row
    assert row.status == "Supported"


def test_most_active_subsystems_marks_multiple_maintainers(seeded_db):
    """Multiple M: rows → multiple_maintainers True so the card
    can render the "et al." suffix."""
    with seeded_db() as s:
        _add_subsystem(
            s, "NET", "Maintained",
            files=["net/"],
            maintainers=[
                ("M", "Jakub Kicinski", "kuba@kernel.org"),
                ("M", "Paolo Abeni", "pabeni@redhat.com"),
            ],
        )
        _add_recent_thread_root(s, "net@x", ["net/core/dev.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = most_active_subsystems_in_inbox(s, alpha, force=True)
    assert out[0].multiple_maintainers is True
    # First M: by id order wins the display slot.
    assert out[0].maintainer_name == "Jakub Kicinski"


def test_most_active_subsystems_carries_sparkline(seeded_db):
    """7-day daily-volume sparkline rides on the SubsystemActivity
    row so the front-page card can render it without an extra
    helper call per card."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_thread_root(s, "spark@x", ["fs/bcachefs/super.c"])
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = most_active_subsystems_in_inbox(s, alpha, force=True)
    assert out[0].spark is not None
    assert len(out[0].spark.days) == 7  # 7-day series
