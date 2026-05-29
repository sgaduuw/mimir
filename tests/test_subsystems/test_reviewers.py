"""Tests for mimir/subsystems_dashboard/reviewers.py: per-
reviewer surfaces (`active_reviewers_in_subsystem`,
`articles_reviewed_by`)."""

from datetime import datetime, timezone

from sqlalchemy import select

from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    ArticleTrailer,
    Inbox,
)
from mimir.subsystems_dashboard import (
    active_reviewers_in_subsystem,
    articles_reviewed_by,
)

from tests.test_subsystems._helpers import (
    _add_recent_patch_with_trailers,
    _add_subsystem,
)


def test_active_reviewers_groups_attestations_by_address(seeded_db):
    """One reviewer who appears on multiple patches collapses to a
    single ReviewerStat with summed counts."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_patch_with_trailers(
            s,
            "p1@x",
            ["fs/bcachefs/super.c"],
            [("Reviewed-by", "Alice", "alice@kernel.org")],
        )
        _add_recent_patch_with_trailers(
            s,
            "p2@x",
            ["fs/bcachefs/io.c"],
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
            s,
            "old1@x",
            ["fs/bcachefs/a.c"],
            [("Reviewed-by", "Carol", "carol@kernel.org")],
            days_ago=2,
        )
        _add_recent_patch_with_trailers(
            s,
            "old2@x",
            ["fs/bcachefs/b.c"],
            [("Reviewed-by", "Carol", "carol@kernel.org")],
            days_ago=2,
        )
        # dave: 2 attestations, latest activity today
        _add_recent_patch_with_trailers(
            s,
            "new1@x",
            ["fs/bcachefs/c.c"],
            [("Reviewed-by", "Dave", "dave@kernel.org")],
            days_ago=0,
        )
        _add_recent_patch_with_trailers(
            s,
            "new2@x",
            ["fs/bcachefs/d.c"],
            [("Reviewed-by", "Dave", "dave@kernel.org")],
            days_ago=2,
        )
        # erin: 1 attestation, today, should rank below the two-counters
        _add_recent_patch_with_trailers(
            s,
            "single@x",
            ["fs/bcachefs/e.c"],
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
    excluded, same path-filter posture as the other subsystem
    helpers."""
    with seeded_db() as s:
        sub = _add_subsystem(
            s,
            "BTRFS-MAIN",
            "Maintained",
            files=["fs/btrfs/"],
            excludes=["fs/btrfs/tests/"],
        )
        _add_recent_patch_with_trailers(
            s,
            "main@x",
            ["fs/btrfs/extent.c"],
            [("Reviewed-by", "Alice", "alice@kernel.org")],
        )
        _add_recent_patch_with_trailers(
            s,
            "tests@x",
            ["fs/btrfs/tests/runner.c"],
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
            s,
            "a@x",
            ["fs/bcachefs/x.c"],
            [("Reviewed-by", "Alpha-only", "ao@kernel.org")],
            inbox_name="alpha",
        )
        _add_recent_patch_with_trailers(
            s,
            "b@x",
            ["fs/bcachefs/y.c"],
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
            s,
            "wild@x",
            ["arch/x86/cstar/init.c"],
            [("Reviewed-by", "Anyone", "any@kernel.org")],
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = active_reviewers_in_subsystem(s, alpha, sub, force=True)
    assert out == []


# `articles_reviewed_by` integration, slice 3 of #97. Powers the
# per-reviewer `/<inbox>/reviewer/<address>` page.


def test_articles_reviewed_by_plan_drops_materialize(seeded_db):
    """The query must NOT materialise an unfiltered per-article view of
    every inbox-link in the archive. Earlier shape JOINed against a
    derived table that did exactly that, blowing past gunicorn's worker
    timeout on the prod corpus for prolific reviewers (#194). Pin the
    plan so the regression is caught at PR time instead of in
    production cold misses."""
    from sqlalchemy import text

    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        # Mirror the exact SQL `articles_reviewed_by` builds, the
        # cache helper closes over `inbox`/`address_normalized`/`limit`,
        # so we reproduce the bind shape rather than calling through
        # the cached entry point (which would skip the plan we want
        # to inspect).
        plan_rows = s.execute(
            text(
                """
                EXPLAIN QUERY PLAN
                SELECT a.id, a.message_id, a.subject, a.date, t.role,
                       COALESCE(
                           canon.name,
                           (SELECT MIN(i.name)
                            FROM article_lists al2
                            JOIN inboxes i ON i.id = al2.inbox_id
                            WHERE al2.article_id = a.id)
                       ) AS inbox_name
                FROM article_trailers t
                JOIN articles a ON a.id = t.article_id
                JOIN article_lists al ON al.article_id = a.id
                LEFT JOIN inboxes canon ON canon.id = a.canonical_inbox_id
                WHERE al.inbox_id = :inbox_id
                  AND t.address_normalized = :addr
                ORDER BY a.date DESC LIMIT :limit
                """
            ),
            {"inbox_id": alpha.id, "addr": "x@y", "limit": 100},
        ).all()
    plan = "\n".join(r[-1] for r in plan_rows)
    assert "MATERIALIZE" not in plan, (
        f"unfiltered MATERIALIZE crept back into the reviewer query plan:\n{plan}"
    )


def test_articles_reviewed_by_caches_for_one_hour(seeded_db):
    """Regression for 1.42.0's wrong-TTL bug: the helper was wired
    to `ACTIVE_THREADS_CACHE_TTL_SEC` (300 s), shorter than the
    warm cycle's `refresh_window` of 450 s. Every warm tick then
    recomputed the full per-reviewer fan-out, costing ~6 minutes
    of broker compute per minute on the production lkml corpus.

    Pin to `SUBSYSTEM_DASHBOARD_CACHE_TTL_SEC` (1 hour, matching
    the rest of the per-subsystem dashboard fan-out the same warm
    cycle drives through this helper). 1 hour is comfortably above
    450 s, so an `articles_reviewed_by` row sits in cache for ~58
    min of each hour and only refreshes near expiry."""
    import time

    from sqlalchemy import select

    from mimir.cache import _ns
    from mimir.models import CacheEntry

    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        # Address with no trailers seeded is fine; the helper still
        # caches the empty list and we're inspecting the TTL the
        # cache row was set with.
        articles_reviewed_by(s, alpha, "noone@example.com", limit=100)
        nskey = _ns("articles_reviewed_by:alpha:noone@example.com:100")
        row = s.execute(select(CacheEntry).where(CacheEntry.key == nskey)).scalar_one()

    delta = row.expires_at - int(time.time())
    assert 3500 < delta < 3700, (
        f"articles_reviewed_by TTL must be ~1h ({3600}s), got {delta}s. "
        "Pre-1.42.1 this was wired to ACTIVE_THREADS_CACHE_TTL_SEC (300s) "
        "which sat inside the warm cycle's refresh_window (450s) and "
        "caused every warm tick to recompute the full fan-out."
    )


def test_articles_reviewed_by_canonical_null_uses_alphabetical_fallback(
    seeded_db,
):
    """Cross-posted article with canonical_inbox_id = NULL: the
    fallback inbox name must be the alphabetically-first linked
    inbox (matches `_canonical_inbox_name` in mimir.web.urls), not
    the inbox we happen to be querying from. Pinned because the
    #194 query rewrite touched this code path."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        art = Article(
            message_id="null-canon@x",
            subject="cross-post, no canonical pinned",
            author="a@example",
            date=datetime.now(timezone.utc),
            thread_parent=None,
            subject_normalized="cross-post no canonical pinned",
            canonical_inbox_id=None,
            lists=[
                ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="a" * 40),
                ArticleList(inbox_id=beta.id, epoch="0.git", commit_sha="b" * 40),
            ],
            files=[ArticleFile(path="fs/bcachefs/x.c")],
            trailers=[
                ArticleTrailer(
                    role="Reviewed-by",
                    name="A",
                    address="a@kernel.org",
                    address_normalized="a@kernel.org",
                )
            ],
        )
        s.add(art)
        s.commit()
        # Query from beta; fallback should still resolve to alpha
        # (the alphabetically-first linked inbox), not beta.
        out = articles_reviewed_by(s, beta, "a@kernel.org", force=True)
    assert len(out) == 1
    assert out[0].inbox_name == "alpha"


def test_articles_reviewed_by_returns_one_entry_per_attestation(seeded_db):
    """Same person under two roles on one patch (Reported-by +
    Tested-by) shows as two ReviewEntry rows. Accurate to the source
    trailer block; the per-reviewer page exists to surface every
    attestation this person made."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_patch_with_trailers(
            s,
            "p1@x",
            ["fs/bcachefs/super.c"],
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
    # by address, not by subsystem, the patch happens to be in one
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
            lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="f" * 40)],
            files=[ArticleFile(path="fs/bcachefs/x.c")],
            trailers=[
                ArticleTrailer(
                    role="Reviewed-by",
                    name="Mixed",
                    address="Mixed@KerneL.OrG",
                    address_normalized="mixed@kernel.org",
                )
            ],
        )
        s.add(art)
        s.commit()
        out = articles_reviewed_by(s, inbox, "mixed@kernel.org", force=True)
    assert {e.message_id for e in out} == {"case@x"}


def test_articles_reviewed_by_orders_by_date_desc(seeded_db):
    """Newest attestations surface first, the page is a chronological
    listing, freshest at the top."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_patch_with_trailers(
            s,
            "old@x",
            ["fs/bcachefs/a.c"],
            [("Reviewed-by", "A", "a@kernel.org")],
            days_ago=10,
        )
        _add_recent_patch_with_trailers(
            s,
            "new@x",
            ["fs/bcachefs/b.c"],
            [("Reviewed-by", "A", "a@kernel.org")],
            days_ago=0,
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = articles_reviewed_by(s, alpha, "a@kernel.org", force=True)
    assert [e.message_id for e in out] == ["new@x", "old@x"]


def test_articles_reviewed_by_scoped_to_inbox(seeded_db):
    """Attestations on patches in `beta` don't bleed into `alpha`'s
    reviewer page, the URL is inbox-scoped and the helper joins
    through `article_lists`."""
    with seeded_db() as s:
        _add_subsystem(s, "BCACHEFS", "Supported", files=["fs/bcachefs/"])
        _add_recent_patch_with_trailers(
            s,
            "a-side@x",
            ["fs/bcachefs/x.c"],
            [("Reviewed-by", "A", "a@kernel.org")],
            inbox_name="alpha",
        )
        _add_recent_patch_with_trailers(
            s,
            "b-side@x",
            ["fs/bcachefs/y.c"],
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
                s,
                f"p{i}@x",
                ["fs/bcachefs/x.c"],
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
            trailers=[
                ArticleTrailer(
                    role="Reviewed-by",
                    name="A",
                    address="a@kernel.org",
                    address_normalized="a@kernel.org",
                )
            ],
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
            s,
            "old@x",
            ["fs/bcachefs/a.c"],
            [("Reviewed-by", "Old Name", "person@kernel.org")],
            days_ago=2,
        )
        _add_recent_patch_with_trailers(
            s,
            "new@x",
            ["fs/bcachefs/b.c"],
            [("Reviewed-by", "New Name", "person@kernel.org")],
            days_ago=0,
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        out = active_reviewers_in_subsystem(s, alpha, sub, force=True)
    assert len(out) == 1
    assert out[0].name == "New Name"
    assert out[0].total == 2


# EXPLAIN plan pin (load-bearing for warm-cycle cost).
#
# Sibling shape to the recent_articles / daily_volume pins in
# tests/test_subsystems/test_reads.py: pin the outer-query shape
# of `active_reviewers_in_subsystem` so the IN-list shape (which
# materialised the entire archive's subsystem-paths UNION on
# every cold miss) doesn't sneak back in.


def test_active_reviewers_in_subsystem_uses_date_index_with_exists(seeded_db):
    """`active_reviewers_in_subsystem` must drive on
    `ix_articles_date` in the 30-day window, EXISTS for both
    inbox and path filter, and join `article_trailers` per
    surviving row via `ix_article_trailers_article_id`. Pre-
    rewrite the IN-list shape materialised every article-id in
    the archive matched by the subsystem's F: globs (millions of
    rows for NETWORKING [GENERAL]) just to intersect with the
    in-window inbox slice; that materialisation dominated the
    warm-cycle cold miss on every medium-traffic inbox (~4 s
    per call). Pin the plan so the regression is caught at PR
    time rather than in the 'subsystem dashboards (top 20)'
    broker warm-slow log."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text

    from mimir.subsystems_dashboard._path_filter import (
        _subsystem_path_filter_exists_sql,
    )
    from tests.test_subsystems._helpers import _add_subsystem

    with seeded_db() as s:
        sub = _add_subsystem(
            s,
            "NETPLAN",
            "Supported",
            files=["net/", "include/linux/skbuff.h"],
            excludes=["net/bluetooth/"],
        )
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        pf = _subsystem_path_filter_exists_sql(sub, prefix="arss")
        assert pf is not None
        path_predicate, path_params = pf
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)
        sql = f"""
            SELECT t.role, t.name, t.address, t.address_normalized,
                   a.date AS art_date
            FROM articles a
            JOIN article_trailers t ON t.article_id = a.id
            WHERE a.date >= :start
              AND EXISTS (
                  SELECT 1 FROM article_lists al
                  WHERE al.article_id = a.id AND al.inbox_id = :inbox_id
              )
              AND {path_predicate}
            ORDER BY a.date DESC
        """
        params = {
            "inbox_id": alpha.id,
            "start": start.isoformat(),
            **path_params,
        }
        plan_rows = s.execute(text("EXPLAIN QUERY PLAN " + sql), params).all()
    plan = "\n".join(r[3] for r in plan_rows)
    assert "SCAN articles" not in plan, (
        f"active_reviewers plan fell back to full scan of articles:\n{plan}"
    )
    assert "SCAN article_files" not in plan, (
        f"active_reviewers plan fell back to full scan of article_files:\n{plan}"
    )
    assert "SCAN article_lists" not in plan, (
        f"active_reviewers plan fell back to full scan of article_lists:\n{plan}"
    )
    assert "SCAN article_trailers" not in plan, (
        f"active_reviewers plan fell back to full scan of article_trailers:\n{plan}"
    )
    assert "ix_articles_date" in plan, (
        f"active_reviewers plan does not use ix_articles_date:\n{plan}"
    )
    assert "MATERIALIZE" not in plan, (
        f"active_reviewers plan re-introduced an unfiltered MATERIALIZE:\n{plan}"
    )


# `most_active_subsystems_in_inbox` + `most_active_subsystems_global`
# integration tests. Powers the subsystem discoverability surfaces
# on the inbox dashboard and the front page.
