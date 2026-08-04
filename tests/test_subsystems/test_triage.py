"""Tests for the per-subsystem triage queues (issue #209):
`needs_attention_patches_in_subsystem` and
`quiet_patches_in_subsystem`. Pinning behaviour across the
inclusion/exclusion matrix the issue specifies (trailers vs none,
mainline vs none, later-version vs none, replies vs none,
maintainer-Ack vs not), plus an EXPLAIN-plan pin so a regression
that lost index-driven access surfaces in CI rather than as a
production cold-miss."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    ArticleTrailer,
    Inbox,
    MainlineCommit,
)
from mimir.subsystems_dashboard.triage import (
    needs_attention_patches_in_subsystem,
    quiet_patches_in_subsystem,
)

from tests.test_subsystems._helpers import _add_subsystem


def _alpha(session):
    return session.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()


def _add_article(
    session,
    msgid,
    *,
    paths,
    days_ago,
    trailers=(),
    inbox_name="alpha",
    thread_parent=None,
    patch_series_key=None,
    patch_series_version=None,
):
    """Create one article with the bits the triage queries care
    about. `trailers` is a list of (role, address) tuples."""
    inbox = session.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
    art = Article(
        message_id=msgid,
        subject=f"patch {msgid}",
        author="author@example.com",
        date=datetime.now(timezone.utc) - timedelta(days=days_ago),
        thread_parent=thread_parent,
        subject_normalized=f"patch {msgid}",
        canonical_inbox_id=inbox.id,
        patch_series_key=patch_series_key,
        patch_series_version=patch_series_version,
        lists=[ArticleList(inbox_id=inbox.id, epoch="0.git", commit_sha="f" * 40)],
        files=[ArticleFile(path=p) for p in paths],
        trailers=[
            ArticleTrailer(
                role=role,
                name="reviewer",
                address=addr,
                address_normalized=addr.lower(),
            )
            for role, addr in trailers
        ],
    )
    session.add(art)
    session.flush()
    return art


# needs_attention contract


def test_needs_attention_includes_review_trailered_patch(seeded_db):
    """The canonical needs-attention shape: patch in subsystem
    paths, has a Reviewed-by, no mainline landing, no later
    version, older than the threshold. Must surface."""
    with seeded_db() as s:
        sub = _add_subsystem(
            s,
            "NETPLAN",
            "Supported",
            files=["net/"],
        )
        s.commit()
        _add_article(
            s,
            "needs-1@x",
            paths=["net/core/dev.c"],
            days_ago=20,
            trailers=[("Reviewed-by", "rev@example.com")],
        )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [p.message_id for p in result] == ["needs-1@x"]
    assert result[0].trailer_summary == "1 Reviewed-by"


def test_needs_attention_excludes_too_recent(seeded_db):
    """A patch younger than `days` shouldn't surface even if every
    other condition matches: gives the patch time to be picked up."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _add_article(
            s,
            "fresh@x",
            paths=["net/core/dev.c"],
            days_ago=2,
            trailers=[("Reviewed-by", "rev@example.com")],
        )
        s.commit()
        result = needs_attention_patches_in_subsystem(
            s,
            _alpha(s),
            sub,
            days=14,
            limit=10,
        )
    assert result == []


def test_needs_attention_excludes_no_trailers(seeded_db):
    """Without review trailers, the patch belongs in the *quiet*
    list, not needs-attention."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _add_article(
            s,
            "untrailered@x",
            paths=["net/core/dev.c"],
            days_ago=20,
        )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert result == []


def test_needs_attention_excludes_landed_in_mainline(seeded_db):
    """A patch that's already in mainline_commits has been picked
    up; no further attention needed."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _add_article(
            s,
            "landed@x",
            paths=["net/core/dev.c"],
            days_ago=20,
            trailers=[("Reviewed-by", "rev@example.com")],
        )
        s.add(
            MainlineCommit(
                commit_sha="a" * 40,
                message_id="landed@x",
                tree_name="linux.git",
                committed_at=datetime.now(timezone.utc),
            )
        )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert result == []


def test_needs_attention_excludes_superseded(seeded_db):
    """When a later revision of the same patch series has been
    posted, the older revision is superseded and shouldn't surface.
    Same-key, later date is the supersession signal (date-ordered,
    not version-string-ordered, sidesteps v10 < v2 lex issues)."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _add_article(
            s,
            "v1@x",
            paths=["net/core/dev.c"],
            days_ago=25,
            patch_series_key="key1",
            patch_series_version="v1",
            trailers=[("Reviewed-by", "rev@example.com")],
        )
        _add_article(
            s,
            "v2@x",
            paths=["net/core/dev.c"],
            days_ago=20,
            patch_series_key="key1",
            patch_series_version="v2",
            trailers=[("Reviewed-by", "rev@example.com")],
        )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    # Only the newer revision surfaces; the v1 was superseded.
    assert [p.message_id for p in result] == ["v2@x"]


def test_needs_attention_excludes_maintainer_acked(seeded_db):
    """An Acked-by from an address in `subsystem_maintainers` (M:/R:
    role) is the pickup signal: the maintainer has indicated they'll
    merge it. Such patches must drop out of needs-attention even if
    they otherwise qualify."""
    with seeded_db() as s:
        sub = _add_subsystem(
            s,
            "NETPLAN",
            "Supported",
            files=["net/"],
            maintainers=[("M", "Maintainer One", "maint@x.com")],
        )
        s.commit()
        _add_article(
            s,
            "acked@x",
            paths=["net/core/dev.c"],
            days_ago=20,
            trailers=[
                ("Reviewed-by", "rev@example.com"),
                ("Acked-by", "maint@x.com"),
            ],
        )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert result == []


def test_needs_attention_keeps_random_acked(seeded_db):
    """Acked-by from a random non-maintainer address doesn't count
    as pickup. The patch should still surface."""
    with seeded_db() as s:
        sub = _add_subsystem(
            s,
            "NETPLAN",
            "Supported",
            files=["net/"],
            maintainers=[("M", "Maintainer One", "maint@x.com")],
        )
        s.commit()
        _add_article(
            s,
            "random-acked@x",
            paths=["net/core/dev.c"],
            days_ago=20,
            trailers=[("Acked-by", "random@elsewhere.com")],
        )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [p.message_id for p in result] == ["random-acked@x"]


def test_needs_attention_keeps_maintainer_reviewed(seeded_db):
    """Reviewed-by from a maintainer is feedback, not a pickup
    commitment. The patch is still awaiting an Ack and should
    surface."""
    with seeded_db() as s:
        sub = _add_subsystem(
            s,
            "NETPLAN",
            "Supported",
            files=["net/"],
            maintainers=[("M", "Maintainer One", "maint@x.com")],
        )
        s.commit()
        _add_article(
            s,
            "maint-reviewed@x",
            paths=["net/core/dev.c"],
            days_ago=20,
            trailers=[("Reviewed-by", "maint@x.com")],
        )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [p.message_id for p in result] == ["maint-reviewed@x"]


def test_needs_attention_oldest_first(seeded_db):
    """The list is ordered oldest-first so the most concerning
    entries surface at the top of the rendered card."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _add_article(
            s,
            "newer@x",
            paths=["net/core/a.c"],
            days_ago=16,
            trailers=[("Reviewed-by", "r@x.com")],
        )
        _add_article(
            s,
            "older@x",
            paths=["net/core/b.c"],
            days_ago=30,
            trailers=[("Reviewed-by", "r@x.com")],
        )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [p.message_id for p in result] == ["older@x", "newer@x"]


# quiet contract


def test_quiet_includes_zero_engagement_patch(seeded_db):
    """The canonical quiet shape: in subsystem paths, no trailers,
    no replies, older than threshold."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _add_article(s, "quiet-1@x", paths=["net/core/dev.c"], days_ago=40)
        s.commit()
        result = quiet_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [p.message_id for p in result] == ["quiet-1@x"]


def test_quiet_excludes_patch_with_trailers(seeded_db):
    """Any trailer (even an unrelated one) bumps the patch out of
    quiet, it belongs in needs-attention instead."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _add_article(
            s,
            "with-trailer@x",
            paths=["net/core/dev.c"],
            days_ago=40,
            trailers=[("Reviewed-by", "r@x.com")],
        )
        s.commit()
        result = quiet_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert result == []


def test_quiet_excludes_patch_with_replies(seeded_db):
    """A reply (any article whose thread_parent points at this
    patch) means engagement; the patch isn't quiet."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _add_article(s, "root@x", paths=["net/core/dev.c"], days_ago=40)
        _add_article(
            s,
            "reply@x",
            paths=["net/core/dev.c"],
            days_ago=39,
            thread_parent="root@x",
        )
        s.commit()
        result = quiet_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    # root@x got a reply, so it's out. reply@x has no replies of
    # its own and would surface, except it's in-thread (we test
    # the no-engagement contract, not absolute quietness).
    assert "root@x" not in [p.message_id for p in result]


def test_quiet_excludes_too_recent(seeded_db):
    """Younger than `days` doesn't qualify even with zero
    engagement; the threshold gives the patch time to attract
    review before being flagged."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _add_article(s, "fresh-quiet@x", paths=["net/core/dev.c"], days_ago=10)
        s.commit()
        result = quiet_patches_in_subsystem(
            s,
            _alpha(s),
            sub,
            days=30,
            limit=10,
        )
    assert result == []


def test_quiet_oldest_first(seeded_db):
    """Same ordering contract as needs-attention."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _add_article(s, "newer-quiet@x", paths=["net/core/a.c"], days_ago=35)
        _add_article(s, "older-quiet@x", paths=["net/core/b.c"], days_ago=60)
        s.commit()
        result = quiet_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [p.message_id for p in result] == ["older-quiet@x", "newer-quiet@x"]


# EXPLAIN plan pins (load-bearing for cold-miss latency)


def test_triage_queries_use_date_index_no_full_scans(seeded_db):
    """Both triage queries must drive on `ix_articles_date` ASC
    over a bounded date range (the BETWEEN literal), not
    materialise the full path-filtered article-id set first. The
    earlier `a.id IN (UNION-of-seeks)` shape ran 4-8 s cold on
    NETWORKING [GENERAL]; the EXISTS-shaped path filter + bounded
    date range drops it to ~200 ms. Pin both:
    - driver is `SEARCH a USING INDEX ix_articles_date`
    - no SCAN of `articles` / `article_trailers` / `article_files`
    """
    from mimir.subsystems_dashboard.triage import (
        _candidate_query_needs_attention,
        _candidate_query_quiet,
    )

    with seeded_db() as s:
        sub = _add_subsystem(
            s,
            "NETPLAN",
            "Supported",
            files=["net/", "include/linux/skbuff.h"],
            excludes=["net/bluetooth/"],
        )
        s.commit()
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        inbox = _alpha(s)
        for label, fn in [
            ("needs_attention", _candidate_query_needs_attention),
            ("quiet", _candidate_query_quiet),
        ]:
            built = fn(inbox, sub, cutoff, 10)
            assert built is not None, f"{label}: builder returned None"
            sql, params = built
            # The bounded date range surfaces in the plan as
            # `date>? AND date<?` (BETWEEN compiles to that pair).
            plan_rows = s.execute(
                text("EXPLAIN QUERY PLAN " + sql),
                params,
            ).all()
            plan = "\n".join(r[3] for r in plan_rows)
            assert "SCAN articles" not in plan, (
                f"{label}: full scan of articles in plan:\n{plan}"
            )
            assert "SCAN article_trailers" not in plan, (
                f"{label}: full scan of article_trailers in plan:\n{plan}"
            )
            assert "SCAN article_files" not in plan, (
                f"{label}: full scan of article_files in plan:\n{plan}"
            )
            # Drive must be the date index on `a`.
            assert "ix_articles_date" in plan, (
                f"{label}: plan does not use ix_articles_date:\n{plan}"
            )


def test_quiet_excludes_patch_whose_only_trailer_is_not_a_review_role(seeded_db):
    """`PatchAttention.trailer_summary`'s docstring justifies being
    empty on the quiet list because that list "by definition has no
    trailers". The quiet query's predicate really is
    `NOT EXISTS (SELECT 1 FROM article_trailers ...)` with no role
    filter, and this pins the difference.

    `test_quiet_excludes_patch_with_trailers` uses a `Reviewed-by`,
    which is also in `_REVIEW_TRAILER_ROLES`, so it passes just as
    happily against a narrowed predicate that only excluded review
    roles. `Suggested-by` is indexed (see
    `trailers.INDEXED_TRAILER_ROLES`) but is NOT in the review set, so
    it separates the two readings: under the documented "no trailers"
    rule this patch is not quiet; under a review-roles-only rule it
    would be, and it would then render with an empty summary while
    carrying attestations.
    """
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _add_article(
            s,
            "suggested-only@x",
            paths=["net/core/dev.c"],
            days_ago=40,
            trailers=[("Suggested-by", "s@x.com")],
        )
        s.commit()
        result = quiet_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert result == [], (
        "the quiet list excludes a patch carrying ANY trailer row, not "
        "just the review-role subset"
    )
