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
