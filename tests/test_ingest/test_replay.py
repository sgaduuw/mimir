"""Tests for mimir/ingest/replay.py: `replay_failures`
re-walking persisted `parse_failures` rows after a parser
fix. Covers recovery on fix, still-failing increments,
missing-mirror deferral, cached-repo cleanup, and the
cross-post-link path."""

from pathlib import Path

from dulwich.repo import Repo
from sqlalchemy import func, select

from mimir.ingest import (
    ingest_epoch,
    replay_failures,
)
from mimir.models import (
    Article,
    ArticleList,
    Inbox,
    ParseFailure,
)

from tests.test_ingest._helpers import _alpha, _build_pubinbox_repo, _rfc5322


def test_replay_failures_recovers_on_parser_fix(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    """Ingest with a tight cap -> failure rows. Lift the cap, point
    the inbox's mirror_path at the test repo, replay -> rows cleared
    and articles inserted."""
    import mimir.parser

    alpha = _alpha(seeded_db)
    mirror_root = tmp_path / "alpha-mirror"
    mirror_root.mkdir()
    _build_pubinbox_repo(
        mirror_root / "0.git",
        [
            _rfc5322("a@example.com", body=b"x" * 500),
            _rfc5322("b@example.com", body=b"x" * 500),
        ],
    )

    # Update the seeded inbox row so replay_failures can find the repo.
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(mirror_root)
        s.commit()

    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 200)
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", mirror_root / "0.git", workers=1)

    with seeded_db() as s:
        assert (
            s.execute(
                select(func.count())
                .select_from(ParseFailure)
                .where(ParseFailure.inbox_id == alpha.id)
            ).scalar_one()
            == 2
        )

    # Parser fix: lift the cap, replay.
    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 50_000_000)
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
    result = replay_failures(ix)

    assert result.attempted == 2
    assert result.recovered == 2
    assert result.still_failed == 0
    assert result.skipped == 0

    with seeded_db() as s:
        assert (
            s.execute(
                select(func.count())
                .select_from(ParseFailure)
                .where(ParseFailure.inbox_id == alpha.id)
            ).scalar_one()
            == 0
        )
        # Articles are now inserted + linked.
        for mid in ("a@example.com", "b@example.com"):
            art = s.execute(
                select(Article).where(Article.message_id == mid)
            ).scalar_one()
            s.execute(
                select(ArticleList).where(
                    ArticleList.article_id == art.id,
                    ArticleList.inbox_id == alpha.id,
                )
            ).scalar_one()


def test_replay_failures_still_fails_bumps_attempts(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    """Replay against an unfixed parser: the row's attempts/last_attempt
    advance but the row stays."""
    import mimir.parser

    alpha = _alpha(seeded_db)
    mirror_root = tmp_path / "alpha-mirror"
    mirror_root.mkdir()
    _build_pubinbox_repo(
        mirror_root / "0.git",
        [
            _rfc5322("c@example.com", body=b"x" * 500),
        ],
    )
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(mirror_root)
        s.commit()

    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 200)
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", mirror_root / "0.git", workers=1)

    with seeded_db() as s:
        before = s.execute(
            select(ParseFailure).where(ParseFailure.inbox_id == alpha.id)
        ).scalar_one()
        before_last = before.last_attempt
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()

    # Replay still hits the same too-tight cap.
    result = replay_failures(ix)
    assert result.recovered == 0
    assert result.still_failed == 1

    with seeded_db() as s:
        after = s.execute(
            select(ParseFailure).where(ParseFailure.inbox_id == alpha.id)
        ).scalar_one()
    assert after.attempts == 2
    assert after.last_attempt >= before_last


def test_replay_failures_skips_when_mirror_missing(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    """If the mirror has been wiped (or the row predates the current
    mirror layout), replay reports `skipped` and leaves the row."""
    import mimir.parser

    alpha = _alpha(seeded_db)
    # Set up + seed a failure row, then point mirror_path elsewhere.
    mirror_root = tmp_path / "alpha-mirror"
    mirror_root.mkdir()
    _build_pubinbox_repo(
        mirror_root / "0.git",
        [
            _rfc5322("d@example.com", body=b"x" * 500),
        ],
    )
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(mirror_root)
        s.commit()

    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 200)
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", mirror_root / "0.git", workers=1)

    # Now repoint the inbox at a non-existent mirror.
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(tmp_path / "does-not-exist")
        s.commit()
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()

    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 50_000_000)
    result = replay_failures(ix)
    assert result.attempted == 1
    assert result.skipped == 1
    assert result.recovered == 0
    assert result.still_failed == 0

    with seeded_db() as s:
        assert (
            s.execute(
                select(func.count())
                .select_from(ParseFailure)
                .where(ParseFailure.inbox_id == alpha.id)
            ).scalar_one()
            == 1
        )


def test_replay_failures_closes_cached_repos(seeded_db, tmp_path, monkeypatch):
    """`replay_failures` caches one dulwich `Repo` per epoch to avoid
    re-opening pack files for back-to-back rows in the same epoch.
    `Repo` holds FDs on object packs, refs, and the loose-object dir
    and has no `__del__`, so without an explicit teardown the FDs
    leak until the function-scoped dict is GC'd. On long replays
    spanning many epochs this is observable as FD exhaustion.

    Seed failure rows in two epochs so the cache actually fills with
    more than one entry, then patch `mimir.ingest.Repo` to track
    `close()` invocations. Both cached repos must be closed before
    `replay_failures` returns."""
    import datetime as _dt

    from mimir.ingest import replay as ingest_mod
    import mimir.parser

    alpha = _alpha(seeded_db)
    mirror_root = tmp_path / "alpha-fd"
    mirror_root.mkdir()

    bad = b"From: a@b.example\r\nSubject: no msgid\r\n\r\nbody"
    _build_pubinbox_repo(mirror_root / "0.git", [bad])
    _build_pubinbox_repo(mirror_root / "1.git", [bad])
    sha0 = Repo(str(mirror_root / "0.git")).head().decode()
    sha1 = Repo(str(mirror_root / "1.git")).head().decode()

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(mirror_root)
        now = _dt.datetime.now(_dt.timezone.utc)
        for epoch, sha in [("0.git", sha0), ("1.git", sha1)]:
            s.add(
                ParseFailure(
                    inbox_id=ix.id,
                    epoch=epoch,
                    commit_sha=sha,
                    error_class="ValueError",
                    error_message="seeded",
                    attempts=1,
                    first_seen=now,
                    last_attempt=now,
                )
            )
        s.commit()

    closed: list[str] = []
    original_repo = ingest_mod.Repo

    class TrackedRepo(original_repo):
        def close(self):
            closed.append(str(self.path))
            return super().close()

    monkeypatch.setattr(ingest_mod, "Repo", TrackedRepo)
    # Parser still raises on the seeded blob (no Message-ID), so
    # both rows stay in the still_failed bucket. The point is to
    # exercise the repo_cache cleanup path, not the recovery path.
    _ = mimir.parser  # ensure import order matches sibling tests

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
    result = replay_failures(ix)
    assert result.attempted == 2
    assert result.still_failed == 2

    assert len(closed) == 2, (
        f"expected 2 Repo.close() calls (one per cached epoch); got {len(closed)}: "
        f"{closed!r}"
    )
    closed_basenames = {Path(p).name for p in closed}
    assert closed_basenames == {"0.git", "1.git"}


# Auto-ANALYZE on threshold-crossing ingest


def test_replay_failures_cross_post_links_existing_article(
    seeded_db,
    tmp_path,
    monkeypatch,
    broker_active,
):
    """A message that's already an article in another inbox should
    replay into a new `article_lists` row in the failing inbox, not
    a duplicate Article. Construct the scenario directly:

    1. Ingest a message normally in beta -> articles row + article_lists(beta).
    2. Stage a parse failure in alpha for the same message-id at a
       different commit_sha (simulating the case where alpha briefly
       failed before recovery).
    3. Replay alpha -> the existing Article gains an alpha
       article_lists row; no duplicate Article is inserted.
    """
    import datetime as _dt

    from mimir.models import Article, ArticleList, ParseFailure

    alpha = _alpha(seeded_db)
    with seeded_db() as s:
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()

    msgid = "cross-replay@example.com"
    beta_mirror = tmp_path / "beta-mirror"
    beta_mirror.mkdir()
    _build_pubinbox_repo(beta_mirror / "0.git", [_rfc5322(msgid)])
    alpha_mirror = tmp_path / "alpha-mirror"
    alpha_mirror.mkdir()
    _build_pubinbox_repo(alpha_mirror / "0.git", [_rfc5322(msgid)])

    with seeded_db() as s:
        beta = s.execute(select(Inbox).where(Inbox.id == beta.id)).scalar_one()
        beta.mirror_path = str(beta_mirror)
        s.commit()
        ingest_epoch(s, beta, "0.git", beta_mirror / "0.git", workers=1)

    # Confirm the article exists in beta and is *not* linked to alpha yet.
    with seeded_db() as s:
        art = s.execute(select(Article).where(Article.message_id == msgid)).scalar_one()
        alpha_link = s.execute(
            select(ArticleList).where(
                ArticleList.article_id == art.id,
                ArticleList.inbox_id == alpha.id,
            )
        ).scalar_one_or_none()
        assert alpha_link is None

    # Read the SHA out of alpha's epoch, replay needs a real blob to parse.
    from dulwich.repo import Repo as DulwichRepo

    repo = DulwichRepo(str(alpha_mirror / "0.git"))
    alpha_sha = repo.head().decode()

    # Stage a failure row pointing at alpha's epoch + sha for this msgid.
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(alpha_mirror)
        s.add(
            ParseFailure(
                inbox_id=ix.id,
                epoch="0.git",
                commit_sha=alpha_sha,
                error_class="ValueError",
                error_message="prior failure",
                attempts=1,
                first_seen=_dt.datetime.now(_dt.timezone.utc),
                last_attempt=_dt.datetime.now(_dt.timezone.utc),
            )
        )
        s.commit()

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
    result = replay_failures(ix)
    assert result.attempted == 1
    assert result.recovered == 1
    assert result.still_failed == 0
    assert result.skipped == 0

    # Cross-post branch fired: no duplicate Article, alpha link now exists.
    with seeded_db() as s:
        arts = (
            s.execute(select(Article).where(Article.message_id == msgid))
            .scalars()
            .all()
        )
        assert len(arts) == 1, (
            f"expected 1 Article row for cross-posted msgid, got {len(arts)}"
        )
        # The alpha article_lists row now exists.
        link = s.execute(
            select(ArticleList).where(
                ArticleList.article_id == arts[0].id,
                ArticleList.inbox_id == alpha.id,
            )
        ).scalar_one()
        # ...and carries a materialised thread root. Replay inserts
        # `article_lists` rows directly, so without an explicit resolve
        # they land permanently NULL, even on a corpus the backfill has
        # already swept and that the operator has no reason to sweep
        # again. Each such hole is then inherited by every later reply.
        assert link.thread_root_id == arts[0].id, (
            "replay left the new link's thread root unresolved; a single "
            "message thread must root at itself"
        )
        # And the failure row is gone.
        assert (
            s.execute(
                select(func.count())
                .select_from(ParseFailure)
                .where(ParseFailure.inbox_id == alpha.id)
            ).scalar_one()
            == 0
        )


# Inbox.last_article_date (#216): bumped at ingest-commit time so the
# front-page "Last activity" string doesn't ride the 24h
# `archive_stats` cache window.


def test_replay_resolves_thread_roots_on_the_non_broker_path(
    seeded_db, tmp_path, monkeypatch
):
    """The Session path, which no other replay test reaches.

    Every other test here takes `broker_active`, so `conn_or_session`
    is a Connection and the flush guard is a no-op by construction:
    deleting the flush entirely left the whole suite green. That made
    the fix for the autoflush=False no-op unpinned, which is how it
    would have regressed silently.

    Without `broker_active`, `replay_failures` falls to
    `SessionLocal()`, which is autoflush=False. The root passes are raw
    `text()` UPDATEs, so without an explicit flush they run against a
    database that has not seen the ORM inserts this replay just made,
    and leave the recovered rows permanently unrooted.
    """
    import mimir.parser
    from mimir.broker import _context
    from mimir.models import Article, ArticleList

    alpha = _alpha(seeded_db)
    mirror_root = tmp_path / "np-mirror"
    mirror_root.mkdir()
    _build_pubinbox_repo(
        mirror_root / "0.git", [_rfc5322("np@example.com", body=b"x" * 500)]
    )

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(mirror_root)
        s.commit()

    # Cap the parser so ingest records a failure rather than an article.
    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 200)
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", mirror_root / "0.git", workers=1)
    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 1_000_000)

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()

    # conftest registers an active broker context for the whole test
    # session, so omitting the `broker_active` fixture is NOT enough:
    # `get_active_writer()` still succeeds and replay takes the
    # Connection path. Clear it around the replay call only (ingest
    # above genuinely requires the broker) so the legacy Session branch
    # actually runs. That this is the only way to reach it is why the
    # branch had no coverage.
    saved_pool, saved_writer = _context._active_pool, _context._active_writer
    _context.clear_active()
    try:
        result = replay_failures(ix)
    finally:
        _context.set_active(saved_pool, saved_writer)
    assert result.recovered == 1, result

    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "np@example.com")
        ).scalar_one()
        link = s.execute(
            select(ArticleList).where(
                ArticleList.article_id == art.id,
                ArticleList.inbox_id == alpha.id,
            )
        ).scalar_one()

    assert link.thread_root_id == art.id, (
        "the non-broker replay path left the row unrooted; the raw-SQL "
        "passes ran before the ORM inserts were flushed"
    )


def test_replay_adopts_descendants_that_self_rooted_while_the_parent_failed(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    """A recovered parent must adopt the subtree that self-rooted while
    it was missing.

    The three passes replay drives (`seed_roots`, `propagate`,
    `break_cycle`) all gate on `thread_root_id IS NULL`. A child that
    arrived while its parent was sitting in `parse_failures` correctly
    self-rooted, which makes its row NON-NULL, so every one of those
    passes skips it forever. Replaying the parent then leaves the
    conversation permanently in two pieces: both halves render fine,
    the sitemap advertises the child as a thread root it is not, and no
    shipped command repairs it, because every repair path keys on NULL.

    Ingest already handles exactly this case, via
    `_pending._set_subtree_root`; replay reused only `drive_passes`,
    which has no equivalent half.

    Deliberately a THREE-message thread. Both other replay root tests
    use single-message threads, so the recovered row never has a
    pre-existing descendant, and the entire defect lives in having one.
    The grandchild is here because the bug is a subtree bug, not a
    single-row bug.
    """
    import mimir.parser
    from mimir.models import Article, ArticleList

    alpha = _alpha(seeded_db)
    mirror_root = tmp_path / "adopt-mirror"
    mirror_root.mkdir()
    # Parent oversized so it fails the cap; child and grandchild small
    # so they land normally and self-root against an absent parent.
    _build_pubinbox_repo(
        mirror_root / "0.git",
        [
            _rfc5322("parent@example.com", body=b"x" * 500),
            _rfc5322("child@example.com", in_reply_to="parent@example.com"),
            _rfc5322("grandchild@example.com", in_reply_to="child@example.com"),
        ],
    )

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(mirror_root)
        s.commit()

    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 200)
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", mirror_root / "0.git", workers=1)
    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 1_000_000)

    # Precondition: the parent failed, and the child self-rooted. If
    # this ever stops holding the test is no longer exercising the bug.
    with seeded_db() as s:
        assert (
            s.execute(
                select(func.count())
                .select_from(ParseFailure)
                .where(ParseFailure.inbox_id == alpha.id)
            ).scalar_one()
            == 1
        )
        child = s.execute(
            select(Article).where(Article.message_id == "child@example.com")
        ).scalar_one()
        child_link = s.execute(
            select(ArticleList).where(
                ArticleList.article_id == child.id,
                ArticleList.inbox_id == alpha.id,
            )
        ).scalar_one()
        assert child_link.thread_root_id == child.id, (
            "precondition: the child should self-root while its parent is absent"
        )

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
    result = replay_failures(ix)
    assert result.recovered == 1, result

    with seeded_db() as s:
        ids = {
            mid: s.execute(
                select(Article.id).where(Article.message_id == mid)
            ).scalar_one()
            for mid in (
                "parent@example.com",
                "child@example.com",
                "grandchild@example.com",
            )
        }
        roots = {
            mid: s.execute(
                select(ArticleList.thread_root_id).where(
                    ArticleList.article_id == aid,
                    ArticleList.inbox_id == alpha.id,
                )
            ).scalar_one()
            for mid, aid in ids.items()
        }

    parent_id = ids["parent@example.com"]
    assert roots["parent@example.com"] == parent_id
    assert roots["child@example.com"] == parent_id, (
        "replay left the child rooted at itself; the recovered parent did "
        "not adopt the subtree that self-rooted while it was missing, and "
        "no repair path can reach a non-NULL row"
    )
    assert roots["grandchild@example.com"] == parent_id, (
        "the grandchild was not re-rooted; the adoption must cover the "
        "whole subtree, not just the immediate child"
    )


def test_replay_adopts_descendants_onto_the_real_root_not_the_recovered_row(
    seeded_db, tmp_path, monkeypatch, broker_active
):
    """The recovered article is a MID-THREAD reply, not the root.

    Varies the axis the sibling test above holds fixed. There the
    recovered message had no parent, so "the recovered article's id" and
    "the thread's root" were the same value and any implementation that
    conflated them looked correct. Here an earlier ancestor is already
    present, so the two differ: the waiting subtree must land on the
    ANCESTOR's root, and writing the recovered row's own id instead is a
    non-NULL wrong value, which is the permanently-unrepairable shape
    (every repair pass keys on NULL).

    Confirmed by mutation: invalidating the subtree to `article_id`
    rather than to NULL passes the sibling test and fails this one.
    """
    import mimir.parser
    from mimir.models import Article, ArticleList

    alpha = _alpha(seeded_db)
    mirror_root = tmp_path / "midthread-mirror"
    mirror_root.mkdir()
    _build_pubinbox_repo(
        mirror_root / "0.git",
        [
            _rfc5322("gp@example.com"),
            _rfc5322("mid@example.com", body=b"x" * 500, in_reply_to="gp@example.com"),
            _rfc5322("leaf@example.com", in_reply_to="mid@example.com"),
        ],
    )

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(mirror_root)
        s.commit()

    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 200)
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", mirror_root / "0.git", workers=1)
    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 1_000_000)

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
    result = replay_failures(ix)
    assert result.recovered == 1, result

    with seeded_db() as s:
        ids = {
            mid: s.execute(
                select(Article.id).where(Article.message_id == mid)
            ).scalar_one()
            for mid in ("gp@example.com", "mid@example.com", "leaf@example.com")
        }
        roots = {
            mid: s.execute(
                select(ArticleList.thread_root_id).where(
                    ArticleList.article_id == aid,
                    ArticleList.inbox_id == alpha.id,
                )
            ).scalar_one()
            for mid, aid in ids.items()
        }

    gp_id = ids["gp@example.com"]
    assert roots["gp@example.com"] == gp_id
    assert roots["mid@example.com"] == gp_id, (
        "the recovered reply should inherit its present ancestor's root"
    )
    assert roots["leaf@example.com"] == gp_id, (
        f"the waiting subtree landed on {roots['leaf@example.com']} instead of "
        f"the thread's real root {gp_id}; a non-NULL wrong root is permanent, "
        "because every repair pass skips non-NULL rows"
    )
