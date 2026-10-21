"""Tests for the per-subsystem triage queues (issue #209):
`needs_attention_patches_in_subsystem` and
`quiet_patches_in_subsystem`. Pinning behaviour across the
inclusion/exclusion matrix the issue specifies (trailers vs none,
mainline vs none, later-version vs none, replies vs none,
maintainer-Ack vs not), plus an EXPLAIN-plan pin so a regression
that lost index-driven access surfaces in CI rather than as a
production cold-miss."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

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
from tests._helpers import _cache_total, count_steps, planner_stats
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
    patch_series_position=None,
):
    """Create one article with the bits the triage queries care
    about. `trailers` is a list of (role, address) tuples."""
    inbox = session.execute(select(Inbox).where(Inbox.name == inbox_name)).scalar_one()
    art = Article(
        message_id=msgid,
        subject=f"patch {msgid}",
        author="author@example.com",
        date=datetime.now(UTC) - timedelta(days=days_ago),
        thread_parent=thread_parent,
        subject_normalized=f"patch {msgid}",
        canonical_inbox_id=inbox.id,
        patch_series_key=patch_series_key,
        patch_series_version=patch_series_version,
        patch_series_position=patch_series_position,
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


@pytest.mark.parametrize("role", ["Reviewed-by", "Acked-by", "Tested-by"])
def test_needs_attention_includes_review_trailered_patch(seeded_db, role):
    """The canonical needs-attention shape: patch in subsystem
    paths, has review feedback, no mainline landing, no later
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
            trailers=[(role, "rev@example.com")],
        )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [p.message_id for p in result] == ["needs-1@x"]
    assert result[0].trailer_summary == f"1 {role}"


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
                committed_at=datetime.now(UTC),
            )
        )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert result == []


def test_needs_attention_excludes_superseded(seeded_db):
    """When a later revision of the same patch series has been
    posted, the older revision is superseded and shouldn't surface.
    The signal is the lifecycle pill's: same key and position, higher
    revision number (`patch_series.newer_revision_sql`)."""
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
    """The date-first shape (big inboxes, #775) must drive on
    `ix_articles_date` ASC over a bounded date range (the BETWEEN literal), not
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
        _triage_stmt,
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
        cutoff = datetime.now(UTC) - timedelta(days=14)
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
                _triage_stmt("EXPLAIN QUERY PLAN " + sql),
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


def test_needs_attention_keeps_earlier_patches_of_one_series(seeded_db):
    """Since #212 every patch of a series carries the series key, so a
    later patch of the SAME posting is not a newer revision. The
    lifecycle pill's predicate decides: same position, higher version."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        for msgid, position, days_ago in (("p1@x", 1, 21), ("p2@x", 2, 20)):
            _add_article(
                s,
                msgid,
                paths=["net/core/dev.c"],
                days_ago=days_ago,
                patch_series_key="key2",
                patch_series_version="v1",
                patch_series_position=position,
                trailers=[("Reviewed-by", "rev@example.com")],
            )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    # One row for the posting (#686), the oldest patch, counting the other.
    assert [(p.message_id, p.series_more) for p in result] == [("p1@x", 1)]


def test_needs_attention_superseded_by_version_not_date(seeded_db):
    """The revision number decides, not the date: v10 supersedes a v9
    posted after it."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        for msgid, version, days_ago in (("v10@x", "v10", 21), ("v9@x", "v9", 20)):
            _add_article(
                s,
                msgid,
                paths=["net/core/dev.c"],
                days_ago=days_ago,
                patch_series_key="key3",
                patch_series_version=version,
                patch_series_position=1,
                trailers=[("Reviewed-by", "rev@example.com")],
            )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [p.message_id for p in result] == ["v10@x"]


def test_needs_attention_keeps_a_patch_the_next_revision_dropped(seeded_db):
    """v1 had three patches and v2 has two: v1's 3/3 has no newer
    revision at its position, so it stays."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        for msgid, version, position, days_ago in (
            ("v1-3@x", "v1", 3, 22),
            ("v2-1@x", "v2", 1, 21),
            ("v2-2@x", "v2", 2, 20),
        ):
            _add_article(
                s,
                msgid,
                paths=["net/core/dev.c"],
                days_ago=days_ago,
                patch_series_key="key4",
                patch_series_version=version,
                patch_series_position=position,
                trailers=[("Reviewed-by", "rev@example.com")],
            )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [(p.message_id, p.series_more) for p in result] == [
        ("v1-3@x", 0),
        ("v2-1@x", 1),
    ]


def _series(s, key, version, patches, *, trailers=True, days_ago=30):
    """Add one posting of a series: `patches` patches at positions
    1..N, one minute apart, oldest first."""
    for n in range(1, patches + 1):
        _add_article(
            s,
            f"{key}-{version}-{n}@x",
            paths=["net/core/dev.c"],
            days_ago=days_ago - n / 1440,
            patch_series_key=key,
            patch_series_version=version,
            patch_series_position=n,
            trailers=[("Reviewed-by", "rev@example.com")] if trailers else [],
        )


def test_needs_attention_resend_is_one_row(seeded_db):
    """`[PATCH v2]` and `[PATCH RESEND v2]` are both stored as v2:
    one row, the original (#686)."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        for msgid, days_ago in (("orig@x", 21), ("resend@x", 20)):
            _add_article(
                s,
                msgid,
                paths=["net/core/dev.c"],
                days_ago=days_ago,
                patch_series_key="key5",
                patch_series_version="v2",
                patch_series_position=1,
                trailers=[("Reviewed-by", "rev@example.com")],
            )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    # One row; the resent copy is the same patch, not another one.
    assert [(p.message_id, p.series_more) for p in result] == [("orig@x", 0)]


def test_needs_attention_large_series_does_not_crowd_the_list(seeded_db):
    """A 15-patch series older than everything else fills the first
    page of candidates; the list must still fill up from later pages."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _series(s, "big", "v1", 15)
        for n in range(3):
            _add_article(
                s,
                f"solo{n}@x",
                paths=["net/core/dev.c"],
                days_ago=20 - n,
                trailers=[("Reviewed-by", "rev@example.com")],
            )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=3)
    assert [(p.message_id, p.series_more) for p in result] == [
        ("big-v1-1@x", 14),
        ("solo0@x", 0),
        ("solo1@x", 0),
    ]


def test_quiet_series_is_one_row(seeded_db):
    """Every patch of a series replies to the cover, so each looks
    quiet on its own; the list shows the posting once (#686)."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _series(s, "quietkey", "v1", 3, trailers=False, days_ago=60)
        s.commit()
        result = quiet_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [(p.message_id, p.series_more) for p in result] == [("quietkey-v1-1@x", 2)]


def test_quiet_excludes_superseded(seeded_db):
    """A quiet v1 with a v2 at the same position is superseded and
    leaves the list, as on needs-attention and the pill (#688)."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        for msgid, version, days_ago in (("q-v1@x", "v1", 60), ("q-v2@x", "v2", 50)):
            _add_article(
                s,
                msgid,
                paths=["net/core/dev.c"],
                days_ago=days_ago,
                patch_series_key="qkey",
                patch_series_version=version,
                patch_series_position=1,
            )
        s.commit()
        result = quiet_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [p.message_id for p in result] == ["q-v2@x"]


def test_needs_attention_pages_across_a_shared_timestamp(seeded_db):
    """Paging resumes after the last (date, id) seen. A series and a
    later patch with one identical timestamp, split across pages,
    must give each row once and skip none."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _series(s, "tie", "v1", 7)
        _add_article(
            s,
            "after-tie@x",
            paths=["net/core/dev.c"],
            days_ago=30,
            trailers=[("Reviewed-by", "rev@example.com")],
        )
        same = datetime.now(UTC) - timedelta(days=30)
        for a in s.execute(select(Article)).scalars():
            a.date = same
        s.commit()
        # limit=2 gives pages of 7: the series fills page one exactly.
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=2)
    assert [(p.message_id, p.series_more) for p in result] == [
        ("tie-v1-1@x", 6),
        ("after-tie@x", 0),
    ]


def test_needs_attention_series_more_counts_the_whole_posting(seeded_db):
    """The list fills on the series' first patch, mid-page, so its
    siblings on later pages are never fetched; the count is still
    every other patch of the posting."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _add_article(
            s,
            "solo@x",
            paths=["net/core/dev.c"],
            days_ago=40,
            trailers=[("Reviewed-by", "rev@example.com")],
        )
        _series(s, "big", "v1", 15)
        s.commit()
        # limit=2 gives pages of 7: solo + big 1..6 on page one.
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=2)
    assert [(p.message_id, p.series_more) for p in result] == [
        ("solo@x", 0),
        ("big-v1-1@x", 14),
    ]


def test_series_more_counts_patches_that_are_not_listed(seeded_db):
    """ "+N more in this series" counts the posting's other patches,
    not only the ones that would qualify for this list: patch 3 has no
    review trailer and still counts."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _series(s, "part", "v1", 2)
        _add_article(
            s,
            "part-v1-3@x",
            paths=["net/core/dev.c"],
            days_ago=29,
            patch_series_key="part",
            patch_series_version="v1",
            patch_series_position=3,
        )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [(p.message_id, p.series_more) for p in result] == [("part-v1-1@x", 2)]


def _cover(s, key, version, msgid, days_ago=30.01):
    """A cover letter as ingest stores it: position 0, the posting's
    key and version, no `diff --git`, so no ArticleFile rows."""
    _add_article(
        s,
        msgid,
        paths=[],
        days_ago=days_ago,
        patch_series_key=key,
        patch_series_version=version,
        patch_series_position=0,
    )


def test_series_more_does_not_count_the_cover_letter(seeded_db):
    """`[PATCH 0/3]` + three patches: the listed patch has two other
    patches. The cover is not a patch (CHANGELOG: "the posting's other
    patches")."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _cover(s, "cov", "v1", "cov-v1-0@x")
        _series(s, "cov", "v1", 3)
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [(p.message_id, p.series_more) for p in result] == [("cov-v1-1@x", 2)]


def test_series_more_counts_a_resent_patch_once(seeded_db):
    """`[PATCH v2 0/2]` and `[PATCH RESEND v2 0/2]` are one posting
    (#686) of two patches: the listed patch has one other patch, not
    the three other copies (and two covers) stored under the key."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _cover(s, "rs", "v2", "rs-cover-orig@x", days_ago=30.01)
        _series(s, "rs", "v2", 2, days_ago=30)
        _cover(s, "rs", "v2", "rs-cover-resend@x", days_ago=20.01)
        for n in (1, 2):
            _add_article(
                s,
                f"rs-resend-{n}@x",
                paths=["net/core/dev.c"],
                days_ago=20 - n / 1440,
                patch_series_key="rs",
                patch_series_version="v2",
                patch_series_position=n,
                trailers=[("Reviewed-by", "rev@example.com")],
            )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [(p.message_id, p.series_more) for p in result] == [("rs-v2-1@x", 1)]


def _unversioned_pair(s, other):
    """One patch posted as `[PATCH]` (stored v1) and again as `other`
    (`resend` or `rfc`, stored without a number): revision 1 both times."""
    for msgid, version, days_ago in (
        ("first@x", "v1", 21),
        ("second@x", other, 20),
    ):
        _add_article(
            s,
            msgid,
            paths=["net/core/dev.c"],
            days_ago=days_ago,
            patch_series_key="rev1",
            patch_series_version=version,
            patch_series_position=1,
            trailers=[("Reviewed-by", "rev@example.com")],
        )


@pytest.mark.parametrize("other", ["resend", "rfc"])
def test_unversioned_resend_and_rfc_fold_into_v1(seeded_db, other):
    """`version_number_sql` rates `resend` and `rfc` as revision 1, so
    the posting folds on the revision number, not the stored string
    (#691)."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _unversioned_pair(s, other)
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [(p.message_id, p.series_more) for p in result] == [("first@x", 0)]


def test_quiet_unversioned_resend_folds_into_v1(seeded_db):
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        for msgid, version, days_ago in (("q1@x", "v1", 60), ("q2@x", "resend", 59)):
            _add_article(
                s,
                msgid,
                paths=["net/core/dev.c"],
                days_ago=days_ago,
                patch_series_key="qrev",
                patch_series_version=version,
                patch_series_position=1,
            )
        s.commit()
        result = quiet_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [p.message_id for p in result] == ["q1@x"]


def test_different_revisions_stay_two_rows(seeded_db):
    """Folding on the number must not merge v1 with v2: v2 drops
    patch 1, so v1's patch 1 is not superseded and both are listed."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        for msgid, version, position, days_ago in (
            ("old@x", "v1", 1, 21),
            ("new@x", "v2", 2, 20),
        ):
            _add_article(
                s,
                msgid,
                paths=["net/core/dev.c"],
                days_ago=days_ago,
                patch_series_key="two",
                patch_series_version=version,
                patch_series_position=position,
                trailers=[("Reviewed-by", "rev@example.com")],
            )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [p.message_id for p in result] == ["old@x", "new@x"]


def test_series_more_counts_an_unversioned_resend_once(seeded_db):
    """A two-patch v1 resent as `resend`: the listed patch has one
    other patch, not three (#691)."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _series(s, "uv", "v1", 2, days_ago=30)
        _series(s, "uv", "resend", 2, days_ago=20)
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [(p.message_id, p.series_more) for p in result] == [("uv-v1-1@x", 1)]


def test_rfc_before_v1_lists_the_v1_patch(seeded_db):
    """An older `rfc` patch must not hide the `v1` patch it folds with:
    a stored `vN` is the row, whatever the date order (#691)."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        for msgid, version, days_ago in (("rfc@x", "rfc", 30), ("v1@x", "v1", 20)):
            _add_article(
                s,
                msgid,
                paths=["net/core/dev.c"],
                days_ago=days_ago,
                patch_series_key="rf",
                patch_series_version=version,
                patch_series_position=1,
                trailers=[("Reviewed-by", "rev@example.com")],
            )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [p.message_id for p in result] == ["v1@x"]


def test_series_more_counts_only_the_listed_posting(seeded_db):
    """A 3-patch RFC then a 2-patch v1: the row is v1's first patch, and
    "+N more" is v1's one other patch, not the RFC's too (#691)."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _series(s, "mix", "rfc", 3, days_ago=30)
        _series(s, "mix", "v1", 2, days_ago=20)
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [(p.message_id, p.series_more) for p in result] == [("mix-v1-1@x", 1)]


def test_replaced_row_keeps_date_order(seeded_db):
    """The list is oldest first: the `v1` row that replaces an older
    `rfc` row is newer than the unrelated patch listed in between."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        for msgid, key, version, days_ago in (
            ("rfc@x", "rf", "rfc", 30),
            ("other@x", "ot", "v1", 25),
            ("v1@x", "rf", "v1", 20),
        ):
            _add_article(
                s,
                msgid,
                paths=["net/core/dev.c"],
                days_ago=days_ago,
                patch_series_key=key,
                patch_series_version=version,
                patch_series_position=1,
                trailers=[("Reviewed-by", "rev@example.com")],
            )
        s.commit()
        result = needs_attention_patches_in_subsystem(s, _alpha(s), sub, limit=10)
    assert [p.message_id for p in result] == ["other@x", "v1@x"]


# Driving shape: date-first for big inboxes, inbox-first for the rest (#775)


def _seed_two_inboxes(s):
    """Two inboxes, two subsystems, ties on the timestamp, one
    crosspost. Returns the subsystems by name."""
    net = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
    fs = _add_subsystem(s, "FSPLAN", "Supported", files=["fs/"])
    s.commit()
    base = datetime.now(UTC) - timedelta(days=60)
    review = [("Reviewed-by", "rev@example.com")]
    for i in range(12):
        # Even rows carry a review trailer (needs-attention), odd rows
        # none (quiet). Three rows share each timestamp.
        art = _add_article(
            s,
            f"a{i}@x",
            paths=["net/core/dev.c"] if i % 3 else ["fs/ext4/inode.c"],
            days_ago=0,
            trailers=review if i % 2 == 0 else (),
            inbox_name="alpha" if i % 4 else "beta",
        )
        art.date = base + timedelta(days=i // 3)
    # A crosspost: one article in both inboxes.
    cross = _add_article(
        s,
        "cross@x",
        paths=["net/core/dev.c"],
        days_ago=0,
        trailers=review,
    )
    cross.date = base + timedelta(days=1)
    cross.lists.append(
        ArticleList(inbox_id=_inbox(s, "beta").id, epoch="0.git", commit_sha="e" * 40)
    )
    s.commit()
    return {"net": net, "fs": fs}


def _inbox(session, name):
    return session.execute(select(Inbox).where(Inbox.name == name)).scalar_one()


@pytest.mark.parametrize("inbox_name", ["alpha", "beta"])
@pytest.mark.parametrize("sub_name", ["net", "fs"])
@pytest.mark.parametrize(
    "fn", [needs_attention_patches_in_subsystem, quiet_patches_in_subsystem]
)
def test_both_driving_shapes_return_the_same_rows(
    seeded_db, monkeypatch, fn, sub_name, inbox_name
):
    """Same ids in the same order from either shape, across keyset
    pages (a page of 2 forces several round trips over ties)."""
    from mimir.subsystems_dashboard import triage

    monkeypatch.setattr(triage, "_page_size", lambda limit, **_: 2)
    seen = {}
    with seeded_db() as s:
        subs = _seed_two_inboxes(s)
        inbox = _inbox(s, inbox_name)
        for shape in (False, True):
            monkeypatch.setattr(triage, "_inbox_first", lambda _i, shape=shape: shape)
            seen[shape] = [
                p.message_id for p in fn(s, inbox, subs[sub_name], limit=50, force=True)
            ]
    assert seen[True] == seen[False]
    # Not vacuous, and the other inbox's rows stay out. Needs-attention
    # on the net subsystem: alpha's a2 and a10 around the crosspost,
    # beta's a4 and a8 around it (a4 and the crosspost tie, id breaks it).
    expected = {
        (needs_attention_patches_in_subsystem, "net", "alpha"): [
            "a2@x",
            "cross@x",
            "a10@x",
        ],
        (needs_attention_patches_in_subsystem, "net", "beta"): [
            "a4@x",
            "cross@x",
            "a8@x",
        ],
    }
    if (fn, sub_name, inbox_name) in expected:
        assert seen[True] == expected[(fn, sub_name, inbox_name)]


def test_inbox_first_plan_drives_from_the_inbox_index(seeded_db):
    """The inbox-first shape must seek `ix_article_lists_inbox_id`
    for the inbox and fetch `articles` by primary key, not scan
    either table."""
    from mimir.subsystems_dashboard.triage import (
        _candidate_query_needs_attention,
        _candidate_query_quiet,
        _triage_stmt,
    )

    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        cutoff = datetime.now(UTC) - timedelta(days=14)
        for fn in (_candidate_query_needs_attention, _candidate_query_quiet):
            sql, params = fn(_alpha(s), sub, cutoff, 10, inbox_first=True)
            rows = s.execute(_triage_stmt("EXPLAIN QUERY PLAN " + sql), params).all()
            plan = "\n".join(r[3] for r in rows)
            assert "SEARCH al_in USING INDEX ix_article_lists_inbox_id" in plan, plan
            assert "SCAN articles" not in plan, plan
            assert "SCAN article_lists" not in plan, plan


def test_shape_follows_the_inbox_size(seeded_db):
    from mimir.dashboard import INBOX_FIRST_BELOW_ARTICLES, _inbox_first

    with seeded_db() as s:
        inbox = _alpha(s)
        _cache_total(inbox, INBOX_FIRST_BELOW_ARTICLES - 1)
        assert _inbox_first(inbox) is True
        _cache_total(inbox, INBOX_FIRST_BELOW_ARTICLES)
        assert _inbox_first(inbox) is False


def test_unknown_inbox_size_goes_inbox_first(seeded_db):
    """No cached stats: inbox-first, whose worst measured case (5.9 s
    on the biggest list) is under date-first's (14 s on a small one).
    The lookup must not compute the COUNT."""
    from mimir import cache
    from mimir.dashboard import _archive_stats_key, _inbox_first

    with seeded_db() as s:
        inbox = _alpha(s)
        cache.delete(_archive_stats_key(inbox))
        assert _inbox_first(inbox) is True
        assert cache.get(_archive_stats_key(inbox)) is None


# Superseded post-filter (#785)


@pytest.mark.parametrize(
    "fn,trailers",
    [
        (needs_attention_patches_in_subsystem, [("Reviewed-by", "rev@example.com")]),
        (quiet_patches_in_subsystem, []),
    ],
)
@pytest.mark.parametrize("inbox_first", [False, True])
def test_superseded_rows_filling_a_page_do_not_end_the_walk(
    seeded_db, monkeypatch, fn, trailers, inbox_first
):
    """The superseded test runs on each fetched page, not in the SQL,
    so a page made only of superseded v1 rows must not read as the end
    of the candidates: the walk pages on to the v2 rows behind them."""
    from mimir.subsystems_dashboard import triage

    monkeypatch.setattr(triage, "_page_size", lambda limit, **_: 2)
    monkeypatch.setattr(triage, "_inbox_first", lambda _i: inbox_first)
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        for n in range(5):
            _add_article(
                s,
                f"s{n}-v1@x",
                paths=["net/core/dev.c"],
                days_ago=90 - n,
                patch_series_key=f"k{n}",
                patch_series_version="v1",
                patch_series_position=1,
                trailers=trailers,
            )
        for n in range(5):
            _add_article(
                s,
                f"s{n}-v2@x",
                paths=["net/core/dev.c"],
                days_ago=60 - n,
                patch_series_key=f"k{n}",
                patch_series_version="v2",
                patch_series_position=1,
                trailers=trailers,
            )
        s.commit()
        result = fn(s, _alpha(s), sub, limit=50, force=True)
    assert [p.message_id for p in result] == [f"s{n}-v2@x" for n in range(5)]


def _insert_patches(s, patches):
    """Bulk-insert `patches`, dicts of date, path, key, version,
    position and trailer, into the alpha inbox. Bulk because the tests
    using it are about row volume."""
    inbox_id = _alpha(s).id
    articles, lists, files, trailers = [], [], [], []
    for aid, p in enumerate(patches, start=100_000):
        articles.append(
            {
                "id": aid,
                "message_id": f"p{aid}@x",
                "subject": f"patch {aid}",
                "author": "author@example.com",
                "date": p["date"],
                "subject_normalized": f"patch {aid}",
                "canonical_inbox_id": inbox_id,
                "patch_series_key": p.get("key"),
                "patch_series_version": p.get("version"),
                "patch_series_position": p.get("position"),
            }
        )
        lists.append(
            {
                "article_id": aid,
                "inbox_id": inbox_id,
                "epoch": "0.git",
                "commit_sha": "f" * 40,
            }
        )
        files.append({"article_id": aid, "path": p["path"]})
        if p.get("trailer"):
            trailers.append(
                {
                    "article_id": aid,
                    "role": "Reviewed-by",
                    "name": "reviewer",
                    "address": "rev@example.com",
                    "address_normalized": "rev@example.com",
                }
            )
    for model, rows in (
        (Article, articles),
        (ArticleList, lists),
        (ArticleFile, files),
        (ArticleTrailer, trailers),
    ):
        if rows:
            s.execute(model.__table__.insert(), rows)
    s.commit()


def _seed_big_series(s, keys, rows_per_key):
    """`keys` postings of `rows_per_key` patches each, one patch per
    posting carrying a review trailer, dates interleaved across the
    postings so the oldest rows are distinct postings. The point is row
    volume under one series key, as in a stable-review posting."""
    base = datetime.now(UTC) - timedelta(days=60)
    _insert_patches(
        s,
        [
            {
                "date": base + timedelta(seconds=j * keys + k),
                "path": "net/core/dev.c",
                "key": f"big{k}",
                "version": "v1",
                "position": j + 1,
                "trailer": j == 0,
            }
            for k in range(keys)
            for j in range(rows_per_key)
        ],
    )


@pytest.fixture
def no_planner_stats(seeded_db):
    """The step budgets below are plan-dependent, and the shared test
    database carries whatever statistics an earlier test's ANALYZE left."""
    with planner_stats(seeded_db):
        yield


# Measured: the post-filter takes 0.38M (needs-attention) and 0.51M
# (quiet) steps, the check inside the candidate SELECT 4.6M for both.
SUPERSEDED_STEP_BUDGET = 1_000_000


@pytest.mark.parametrize(
    "fn", [needs_attention_patches_in_subsystem, quiet_patches_in_subsystem]
)
def test_triage_work_does_not_grow_with_the_candidate_window(
    seeded_db, no_planner_stats, fn
):
    """The newer-revision check costs a walk of the whole series key,
    so it belongs on the page that is listed, not on every candidate
    in the window. 200 postings of 50 patches: checking each candidate
    in the SQL walks the series 200+ times before the first page
    returns; checking the page does it once per fetched row (200 on
    this inbox-first walk). Counted in
    SQLite VM steps, which do not depend on the machine's speed."""
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _seed_big_series(s, keys=200, rows_per_key=50)
        with count_steps(s) as steps:
            result = fn(s, _alpha(s), sub, limit=10, force=True)
    assert len(result) == 10
    assert steps.count < SUPERSEDED_STEP_BUDGET, steps.count


# Dense superseded rows ahead of the kept ones (#785)


def _seed_dense_superseded(s, *, background, series, versions, trailers):
    now = datetime.now(UTC)
    # Other subsystems' traffic in the window: read by every inbox-first page.
    patches = [
        {"date": now - timedelta(days=100, seconds=i), "path": "drivers/other.c"}
        for i in range(background)
    ]
    # `versions` revisions of each series, oldest revision first in date order.
    patches += [
        {
            "date": now - timedelta(days=100 - 10 * v, seconds=series - k),
            "path": "net/core/dev.c",
            "key": f"ser{k}",
            "version": f"v{v}",
            "position": 1,
            "trailer": trailers,
        }
        for v in range(1, versions + 1)
        for k in range(series)
    ]
    _insert_patches(s, patches)


DENSE_SUPERSEDED_STEP_BUDGET = 4_000_000


@pytest.mark.parametrize("inbox_first", [True, False])
@pytest.mark.parametrize(
    "fn,trailers",
    [
        (needs_attention_patches_in_subsystem, True),
        (quiet_patches_in_subsystem, False),
    ],
)
def test_dense_superseded_rows_do_not_multiply_pages(
    seeded_db, no_planner_stats, monkeypatch, fn, trailers, inbox_first
):
    from mimir.subsystems_dashboard import triage

    monkeypatch.setattr(triage, "_inbox_first", lambda _i: inbox_first)
    with seeded_db() as s:
        sub = _add_subsystem(s, "NETPLAN", "Supported", files=["net/"])
        s.commit()
        _seed_dense_superseded(
            s, background=20_000, series=2000, versions=3, trailers=trailers
        )
        with count_steps(s) as steps:
            result = fn(s, _alpha(s), sub, limit=10, force=True)
        listed = {s.get(Article, p.article_id).patch_series_version for p in result}
    assert len(result) == 10
    # Current revisions only, never a superseded v1 or v2.
    assert listed == {"v3"}
    # Measured, inbox-first (the two small-list cases): the doubling
    # page takes 2.7M steps; a fixed page, whether 20 or 200 rows, past
    # 4000 superseded rows re-reading 20k inbox rows each time, 7.5M.
    # Fixed 20 rows at 200 postings, the first version of this test,
    # took 3.7M to 3.9M.
    assert steps.count < DENSE_SUPERSEDED_STEP_BUDGET, steps.count
