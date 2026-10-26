"""Tests for mimir/subsystems.py: the article-level path-
matching primitives. `path_matches_glob` (directory /
exact-file / wildcard variants), `subsystems_for_paths`
(F:/X: glob resolution), and `recent_patches_touching`
(reverse lookup from article to other patches touching
the same path)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from mimir.models import (
    Article,
    ArticleFile,
    Inbox,
)
from mimir.subsystems import (
    article_paths,
    path_matches_glob,
    recent_patches_touching,
    subsystems_for_paths,
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
        "Documentation/foo.rst",
        "Documentation/bar.rst",
    )


def test_wildcard_glob_via_fnmatch():
    assert path_matches_glob("drivers/net/foo/file.c", "drivers/net/*")
    assert path_matches_glob("drivers/net/foo.c", "drivers/net/*.c")
    # `?` and `[]` also flow through fnmatch.
    assert path_matches_glob("a3.c", "a?.c")
    assert path_matches_glob("aN.c", "a[NM].c")


# `subsystems_for_paths` over an article's `article_paths`, uses the
# seeded DB and inserts MAINTAINERS-shaped fixtures inline.


def _hits_for_article(session, article):
    return subsystems_for_paths(
        session, [row[2] for row in article_paths(session, article) if row[2]]
    )


def test_subsystems_for_paths_directory_match(seeded_db):
    with seeded_db() as s:
        _add_subsystem(
            s,
            "BCACHEFS",
            "Maintained",
            files=["fs/bcachefs/"],
            maintainers=[("M", "Kent Overstreet", "kent.overstreet@linux.dev")],
        )
        art_id = _add_patch_article(s, "p1@x", ["fs/bcachefs/super.c"])
        s.commit()

        hits = _hits_for_article(s, s.get(Article, art_id))
    assert len(hits) == 1
    assert hits[0].name == "BCACHEFS"
    assert hits[0].status == "Maintained"
    assert hits[0].maintainers == [
        ("M", "Kent Overstreet", "kent.overstreet@linux.dev"),
    ]


def test_subsystems_for_paths_multi_subsystem(seeded_db):
    """A patch touching two unrelated subsystems lands both in the
    result, sorted by name for a stable header."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Maintained", files=["fs/bcachefs/"])
        _add_subsystem(s, "BTRFS", "Maintained", files=["fs/btrfs/"])
        art_id = _add_patch_article(
            s,
            "p2@x",
            [
                "fs/bcachefs/io.c",
                "fs/btrfs/extent_io.c",
            ],
        )
        s.commit()
        hits = _hits_for_article(s, s.get(Article, art_id))
    assert [h.name for h in hits] == ["BCACHEFS", "BTRFS"]


def test_subsystems_for_paths_exclude_vetoes_match(seeded_db):
    """`X:` (exclude) entries veto a `F:` match within the same
    subsystem. `fs/btrfs/tests/` should NOT pull in BTRFS-MAIN."""
    with seeded_db() as s:
        _add_subsystem(
            s,
            "BTRFS-MAIN",
            "Maintained",
            files=["fs/btrfs/"],
            excludes=["fs/btrfs/tests/"],
        )
        art_id = _add_patch_article(s, "p3@x", ["fs/btrfs/tests/runner.c"])
        s.commit()
        hits = _hits_for_article(s, s.get(Article, art_id))
    assert hits == []


def test_subsystems_for_paths_exclude_only_vetoes_its_own_subsystem(
    seeded_db,
):
    """X: in subsystem A doesn't affect subsystem B. If A and B both
    cover the path, B's match survives even when A excludes it."""
    with seeded_db() as s:
        _add_subsystem(
            s,
            "BTRFS-MAIN",
            "Maintained",
            files=["fs/btrfs/"],
            excludes=["fs/btrfs/tests/"],
        )
        _add_subsystem(
            s,
            "BTRFS-TESTS",
            "Maintained",
            files=["fs/btrfs/tests/"],
        )
        art_id = _add_patch_article(s, "p4@x", ["fs/btrfs/tests/runner.c"])
        s.commit()
        hits = _hits_for_article(s, s.get(Article, art_id))
    assert [h.name for h in hits] == ["BTRFS-TESTS"]


def test_subsystems_for_paths_no_paths_returns_empty(seeded_db):
    """A non-patch article (no ArticleFile rows) returns no
    subsystems, the cheap-path early return matters at scale."""
    with seeded_db() as s:
        _add_subsystem(s, "BTRFS", "Maintained", files=["fs/btrfs/"])
        art_id = _add_patch_article(s, "p5@x", [])
        s.commit()
        hits = _hits_for_article(s, s.get(Article, art_id))
    assert hits == []


def test_subsystems_for_paths_uses_cached_rule_snapshot(seeded_db):
    """The rule set (~15k rows on the kernel tree) is fetched from
    the cross-process cache, not pulled per call. After a first
    `_hits_for_article` populates the cache, adding a new
    subsystem WITHOUT invalidating the cache must NOT show up in
    a subsequent lookup, the cached snapshot is what's read. This
    pins the caching contract; `invalidate_rules_snapshot()` is
    what the `update-mainline` flow calls to refresh it."""
    from mimir.subsystems import invalidate_rules_snapshot

    with seeded_db() as s:
        _add_subsystem(s, "ORIGINAL", "Maintained", files=["drivers/orig/"])
        art1 = _add_patch_article(s, "p-cache-1@x", ["drivers/orig/x.c"])
        s.commit()

        # Prime the cache.
        first = _hits_for_article(s, s.get(Article, art1))
        assert [h.name for h in first] == ["ORIGINAL"]

        # Add a new subsystem WITHOUT invalidating; the cached
        # snapshot should still be in effect, so the new
        # subsystem must NOT yet show up for an article that
        # touches its path.
        _add_subsystem(s, "STALE", "Maintained", files=["drivers/stale/"])
        art2 = _add_patch_article(s, "p-cache-2@x", ["drivers/stale/y.c"])
        s.commit()
        stale_hits = _hits_for_article(s, s.get(Article, art2))
        assert stale_hits == [], (
            "rule snapshot should still be cached; STALE should not "
            "appear yet without an explicit invalidate"
        )

        # Now invalidate, mirroring what `mainline.load_maintainers`
        # does after a MAINTAINERS reload. The next read picks up
        # the new subsystem.
        invalidate_rules_snapshot()
        fresh_hits = _hits_for_article(s, s.get(Article, art2))
        assert [h.name for h in fresh_hits] == ["STALE"]


# `recent_patches_touching` integration.


def test_recent_patches_touching_returns_others_sharing_path(seeded_db):
    """Two patches touch the same file → the sidebar surfaces the
    other one (not the current article)."""
    with seeded_db() as s:
        a = _add_patch_article(s, "p10@x", ["fs/bcachefs/super.c"])
        b = _add_patch_article(s, "p11@x", ["fs/bcachefs/super.c"])
        s.commit()
        out = recent_patches_touching(s, ["fs/bcachefs/super.c"], exclude_article_id=a)
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
        now = datetime.now(UTC)
        for i, dt in enumerate(
            [
                now - timedelta(days=60),  # oldest
                now - timedelta(days=10),  # newest
                now - timedelta(days=30),  # middle
            ]
        ):
            art = Article(
                message_id=f"date{i}@x",
                subject="x",
                author="a@x",
                date=dt,
                thread_parent=None,
                subject_normalized="x",
                canonical_inbox_id=inbox.id,
                lists=[
                    ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="f" * 40)
                ],
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
            message_id="canon@x",
            subject="x",
            author="a@x",
            date=datetime.now(UTC) - timedelta(days=1),
            thread_parent=None,
            subject_normalized="x",
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
        now = datetime.now(UTC)
        recent_art = Article(
            message_id="recent@x",
            subject="x",
            author="a@x",
            date=now - timedelta(days=10),
            thread_parent=None,
            subject_normalized="x",
            canonical_inbox_id=inbox.id,
            lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="a" * 40)],
            files=[ArticleFile(path="fs/popular.c")],
        )
        ancient_art = Article(
            message_id="ancient@x",
            subject="x",
            author="a@x",
            date=now - timedelta(days=settings.recent_patches_max_age_days + 30),
            thread_parent=None,
            subject_normalized="x",
            canonical_inbox_id=inbox.id,
            lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="b" * 40)],
            files=[ArticleFile(path="fs/popular.c")],
        )
        s.add_all([recent_art, ancient_art])
        s.commit()
        out = recent_patches_touching(
            s,
            ["fs/popular.c"],
            exclude_article_id=-1,
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
            {"min_date": (datetime.now(UTC) - timedelta(days=180)).isoformat()},
        ).all()
        plan = "\n".join(r[3] for r in plan_rows)

    assert "SCAN articles" not in plan, f"full scan of articles in plan:\n{plan}"
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


# Indexed matching (#705): same answers as the brute-force rule walk,
# without decoding and walking ~10k rules per call.


def _reference(rules, paths):
    """The pre-#705 algorithm, kept as the oracle: a subsystem matches
    when some path matches one of its F: rules and none of its X:."""
    from collections import defaultdict

    from mimir.subsystems import path_matches_glob

    inc, exc = defaultdict(list), defaultdict(list)
    for name, glob, is_exclude in rules:
        (exc if is_exclude else inc)[name].append(glob)
    return sorted(
        name
        for name, globs in inc.items()
        if any(
            any(path_matches_glob(p, g) for g in globs)
            and not any(path_matches_glob(p, x) for x in exc.get(name, ()))
            for p in paths
        )
    )


_TRICKY_RULES = [
    ("THE REST", "*", False),
    ("THE REST", "*/", False),
    ("NETWORKING [GENERAL]", "net/", False),
    ("NETWORKING [GENERAL]", "include/net/", False),
    ("NETWORKING DRIVERS", "drivers/net/", False),
    ("NETWORKING DRIVERS", "drivers/net/wireless/", True),
    ("MLX5", "drivers/net/ethernet/mellanox/mlx5/core/", False),
    ("WIRELESS", "drivers/net/wireless/", False),
    ("DT BINDINGS", "Documentation/devicetree/bindings/*", False),
    ("WILDCARD FIRST", "*/qcom/*", False),
    ("EXACT FILE", "MAINTAINERS", False),
    ("CHARCLASS", "arch/[ax]86/kernel/", False),
]
_TRICKY_PATHS = [
    ["drivers/net/ethernet/mellanox/mlx5/core/en.c", "include/net/x.h"],
    ["drivers/net/wireless/ath/a.c"],
    ["net/core/dev.c"],
    ["Documentation/devicetree/bindings/net/x.yaml"],
    ["arch/arm64/boot/dts/qcom/x.dts"],
    ["MAINTAINERS"],
    ["drivers/net"],
    ["arch/x86/kernel/a.c", "scripts/checkpatch.pl"],
    ["netfilter/foo.c"],
]


def _seed_rules(s, rules):
    by_name = {}
    for name, glob, is_exclude in rules:
        by_name.setdefault(name, ([], []))[1 if is_exclude else 0].append(glob)
    for name, (files, excludes) in by_name.items():
        _add_subsystem(s, name, "Maintained", files=files, excludes=excludes)


def test_indexed_match_agrees_with_the_rule_walk_on_tricky_shapes(seeded_db):
    with seeded_db() as s:
        _seed_rules(s, _TRICKY_RULES)
        ids = [
            _add_patch_article(s, f"tricky{n}@x", paths)
            for n, paths in enumerate(_TRICKY_PATHS)
        ]
        s.commit()
        got = [
            sorted(h.name for h in _hits_for_article(s, s.get(Article, i))) for i in ids
        ]
    want = [_reference(_TRICKY_RULES, paths) for paths in _TRICKY_PATHS]
    assert any(len(w) > 2 for w in want)  # precondition: real overlap exercised
    assert got == want


def test_indexed_match_agrees_with_the_rule_walk_on_random_rules(seeded_db):
    """Randomised: 2,000 rule-and-path combinations from a small path
    vocabulary, so prefixes, wildcards and exclusions collide often."""
    import random

    rng = random.Random(705)
    parts = ["drivers", "net", "gpu", "qcom", "x", "core", "include"]

    def path(depth):
        return "/".join(rng.choice(parts) for _ in range(depth))

    rules = []
    for n in range(60):
        name = f"SUB {n}"
        for _ in range(rng.randint(1, 4)):
            shape = rng.choice(["dir", "file", "star", "lead", "q"])
            p = path(rng.randint(1, 3))
            glob = {
                "dir": p + "/",
                "file": p + ".c",
                "star": p + "/*",
                "lead": "*/" + p.split("/")[-1] + "*",
                "q": p + "?.c",
            }[shape]
            rules.append((name, glob, rng.random() < 0.15))
    paths = [
        [path(rng.randint(1, 4)) + rng.choice(["", ".c", "x.c"])] for _ in range(200)
    ]
    with seeded_db() as s:
        _seed_rules(s, rules)
        ids = [_add_patch_article(s, f"rand{n}@x", p) for n, p in enumerate(paths)]
        s.commit()
        got = [
            sorted(h.name for h in _hits_for_article(s, s.get(Article, i))) for i in ids
        ]
    want = [_reference(rules, p) for p in paths]
    assert sum(1 for w in want if w) > 50  # precondition: most paths match something
    assert got == want


def test_rules_index_is_built_once_per_rules_version(seeded_db, monkeypatch):
    """The index lives per process, keyed by the rules version
    (`mainline_state.last_commit_sha` for linus, which every MAINTAINERS
    rebuild changes) and valid only while the cached snapshot is. A new
    version, an invalidated snapshot, or no version rebuilds it."""
    from mimir import subsystems
    from mimir.models import MainlineState

    builds = []
    real_build = subsystems._RulesIndex.build

    def counting(snap):
        builds.append(1)
        return real_build(snap)

    monkeypatch.setattr(subsystems._RulesIndex, "build", staticmethod(counting))
    with seeded_db() as s:
        _add_subsystem(s, "NET", "Maintained", files=["net/"])
        art = _add_patch_article(s, "idx@x", ["net/a.c"])
        s.add(MainlineState(tree_name="linus", last_commit_sha="a" * 40))
        s.commit()

        for _ in range(3):
            _hits_for_article(s, s.get(Article, art))
        assert len(builds) == 1

        s.get(MainlineState, "linus").last_commit_sha = "b" * 40
        s.commit()
        _hits_for_article(s, s.get(Article, art))
        assert len(builds) == 2

        subsystems.invalidate_rules_snapshot()
        _hits_for_article(s, s.get(Article, art))
        assert len(builds) == 3

        s.get(MainlineState, "linus").last_commit_sha = None
        s.commit()
        _hits_for_article(s, s.get(Article, art))
        _hits_for_article(s, s.get(Article, art))
        assert len(builds) == 5


def _names(s, art):
    return sorted(h.name for h in _hits_for_article(s, s.get(Article, art)))


def test_index_does_not_outlive_a_reload_seen_mid_invalidation(seeded_db):
    """A MAINTAINERS reload commits rules and version together, then
    drops the cached snapshot in a later write. A reader in between sees
    the new version with the old snapshot; its index must not survive
    once the reload finishes (found in the 3.17.0 release review)."""
    from mimir import subsystems
    from mimir.models import MainlineState

    with seeded_db() as s:
        _add_subsystem(s, "NET", "Maintained", files=["net/"])
        art = _add_patch_article(s, "reload@x", ["drivers/x/a.c"])
        s.add(MainlineState(tree_name="linus", last_commit_sha="a" * 40))
        s.commit()

        # This process builds and keeps its index at version a.
        assert _names(s, art) == []

        # The reload's first write: new rules and new version, one commit.
        _add_subsystem(s, "DRV", "Maintained", files=["drivers/x/"])
        s.get(MainlineState, "linus").last_commit_sha = "b" * 40
        s.commit()

        # A request here, before the invalidation lands, rebuilds from the
        # still-cached old snapshot. (Wrong for this one request; the
        # pre-3.17 code had the same one-request window.)
        _names(s, art)

        # The reload's second write.
        subsystems.invalidate_rules_snapshot()

        # Another process (its own empty memo) serves a request and
        # recomputes the snapshot row from the new rules.
        mine = getattr(subsystems, "_index", None)
        subsystems._index = None
        assert _names(s, art) == ["DRV"]  # precondition: new rules visible
        subsystems._index = mine

        # The reload is complete. This process must see the new rules too.
        assert _names(s, art) == ["DRV"]


def test_a_stale_snapshot_written_back_after_a_reload_is_not_used(seeded_db):
    """A slow reader that computed the snapshot from the old rules can
    write it back after the reload's delete. Its version is older than
    the rules', so it is recomputed rather than matched against."""
    from mimir import cache, subsystems
    from mimir.models import MainlineState

    with seeded_db() as s:
        _add_subsystem(s, "NET", "Maintained", files=["net/"])
        art = _add_patch_article(s, "late@x", ["drivers/x/a.c"])
        s.add(MainlineState(tree_name="linus", last_commit_sha="a" * 40))
        s.commit()
        old = subsystems._compute_rules_snapshot()
        assert old.version == "a" * 40  # precondition

        _add_subsystem(s, "DRV", "Maintained", files=["drivers/x/"])
        s.get(MainlineState, "linus").last_commit_sha = "b" * 40
        s.commit()
        subsystems.invalidate_rules_snapshot()
        cache.set(subsystems._RULES_CACHE_KEY, old, ttl=3600)  # the late write
        subsystems._index = None

        assert _names(s, art) == ["DRV"]
