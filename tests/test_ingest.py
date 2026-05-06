"""Outcome-bucket contract for `mimir.ingest`.

Every commit walked must land in exactly one of:
    new / linked / dup_batch / dup_db / failed

This file pins each bucket via a tiny ephemeral public-inbox-shaped
git repo built with dulwich, then drives `ingest_epoch` against it
with workers=1 (deterministic, stays in-process).
"""
from datetime import datetime
from pathlib import Path

from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo
from sqlalchemy import delete, func, select

from mimir.ingest import (
    MIN_PROMOTE_OBSERVATIONS,
    _maybe_promote_list_address,
    ingest_epoch,
    ingest_inbox,
    replay_failures,
)
from mimir.models import (
    Article,
    ArticleList,
    Inbox,
    InboxAddressObservation,
    IngestState,
    ParseFailure,
)


def _rfc5322(msgid: str, body: bytes = b"hello", to: str | None = None, cc: str | None = None) -> bytes:
    """Minimal valid RFC 5322 message. Optional To/Cc let canonical-
    inbox tests inject list addresses."""
    parts = [
        b"Message-ID: <" + msgid.encode() + b">\r\n",
        b"From: a@b.example\r\n",
    ]
    if to is not None:
        parts.append(b"To: " + to.encode() + b"\r\n")
    if cc is not None:
        parts.append(b"Cc: " + cc.encode() + b"\r\n")
    parts.extend([
        b"Subject: t\r\n",
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n",
        b"\r\n",
        body,
    ])
    return b"".join(parts)


def _build_pubinbox_repo(repo_path: Path, messages: list[bytes]) -> Path:
    """Build a bare public-inbox v2-shaped repo: one commit per
    message, the tree carries an `m` blob with the raw RFC 5322
    bytes. Returns the repo path."""
    repo = Repo.init_bare(str(repo_path), mkdir=True)
    parent = None
    for i, raw in enumerate(messages):
        blob = Blob.from_string(raw)
        repo.object_store.add_object(blob)

        tree = Tree()
        tree.add(b"m", 0o100644, blob.id)
        repo.object_store.add_object(tree)

        commit = Commit()
        commit.tree = tree.id
        commit.parents = [parent] if parent else []
        commit.author = commit.committer = b"test <t@x>"
        commit.commit_time = commit.author_time = 1700000000 + i
        commit.commit_timezone = commit.author_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = f"add message {i}".encode()
        repo.object_store.add_object(commit)
        parent = commit.id

    if parent is not None:
        repo.refs[b"HEAD"] = parent
    return repo_path


def _alpha(seeded_db) -> Inbox:
    with seeded_db() as s:
        return s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()


# Bucket: new


def test_ingest_new_message_creates_article(seeded_db, tmp_path):
    alpha = _alpha(seeded_db)
    _build_pubinbox_repo(tmp_path / "0.git", [_rfc5322("fresh@example.com")])

    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.new == 1
    assert result.linked == 0
    assert result.dup_batch == 0
    assert result.dup_db == 0
    assert result.failed == 0

    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "fresh@example.com")
        ).scalar_one()
        link = s.execute(
            select(ArticleList).where(
                ArticleList.article_id == art.id,
                ArticleList.inbox_id == alpha.id,
            )
        ).scalar_one()
        assert link.epoch == "0.git"


# Bucket: linked (cross-post)


def test_ingest_linked_when_message_id_already_in_other_inbox(seeded_db, tmp_path):
    """art2@example.com is in beta (seeded). Ingesting it into alpha
    must reuse the existing Article and add an article_lists row —
    that's the `linked` bucket."""
    alpha = _alpha(seeded_db)
    _build_pubinbox_repo(tmp_path / "0.git", [_rfc5322("art2@example.com")])

    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.linked == 1
    assert result.new == 0
    assert result.dup_batch == 0
    assert result.dup_db == 0

    with seeded_db() as s:
        # Same Article, now linked to both inboxes.
        art = s.execute(
            select(Article).where(Article.message_id == "art2@example.com")
        ).scalar_one()
        inbox_ids = {
            row.inbox_id for row in s.execute(
                select(ArticleList).where(ArticleList.article_id == art.id)
            ).scalars()
        }
        assert len(inbox_ids) == 2  # beta (seed) + alpha (this ingest)


# Bucket: dup_batch


def test_ingest_dup_batch_skips_in_same_walk(seeded_db, tmp_path):
    """Two commits in the same batch carrying the same Message-ID:
    the second lands in dup_batch."""
    alpha = _alpha(seeded_db)
    _build_pubinbox_repo(tmp_path / "0.git", [
        _rfc5322("twin@example.com", body=b"first"),
        _rfc5322("twin@example.com", body=b"second"),
    ])

    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.new == 1
    assert result.dup_batch == 1
    assert result.linked == 0
    assert result.dup_db == 0
    assert result.failed == 0


# Bucket: dup_db (re-walk)


def test_ingest_dup_db_when_rewalking_existing_inbox(seeded_db, tmp_path):
    """Run ingest_epoch twice against the same repo. After the
    first run, the article is in DB and linked to alpha; the second
    run, with rewound IngestState, sees it as `dup_db`."""
    alpha = _alpha(seeded_db)
    _build_pubinbox_repo(tmp_path / "0.git", [_rfc5322("once@example.com")])

    with seeded_db() as s:
        first = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)
    assert first.new == 1

    # Rewind state so the walker re-emits the same commit.
    with seeded_db() as s:
        state = s.execute(
            select(IngestState).where(
                IngestState.inbox_id == alpha.id,
                IngestState.epoch == "0.git",
            )
        ).scalar_one()
        state.last_commit_sha = None
        s.commit()

    with seeded_db() as s:
        second = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert second.dup_db == 1
    assert second.new == 0
    assert second.linked == 0
    assert second.dup_batch == 0


# Bucket: failed


def test_ingest_failed_on_unparseable_message(seeded_db, tmp_path):
    """A message with no Message-ID raises ValueError inside
    parse_message, gets caught by the worker, counted as `failed`,
    and the walker advances past it."""
    alpha = _alpha(seeded_db)
    bad = b"From: a@b.example\r\nSubject: no msgid\r\n\r\nbody"
    _build_pubinbox_repo(tmp_path / "0.git", [
        _rfc5322("good@example.com"),
        bad,
    ])

    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.new == 1
    assert result.failed == 1
    assert result.dup_batch == 0
    assert result.dup_db == 0
    assert result.linked == 0


def test_ingest_failed_on_oversized_message(seeded_db, tmp_path, monkeypatch):
    """A message above the size cap raises MessageTooLarge in
    parse_message; counted as failed."""
    import mimir.parser

    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 200)

    alpha = _alpha(seeded_db)
    huge_body = b"x" * 500
    _build_pubinbox_repo(tmp_path / "0.git", [
        _rfc5322("over@example.com", body=huge_body),
    ])

    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.failed == 1
    assert result.new == 0


# IngestState resume


def test_ingest_state_advances_on_each_batch(seeded_db, tmp_path):
    """After ingest, IngestState.last_commit_sha equals the HEAD of
    the repo we just walked."""
    alpha = _alpha(seeded_db)
    repo_path = tmp_path / "0.git"
    _build_pubinbox_repo(repo_path, [_rfc5322(f"m{i}@example.com") for i in range(3)])

    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", repo_path, workers=1)

    head = Repo(str(repo_path)).head().decode()
    with seeded_db() as s:
        state = s.execute(
            select(IngestState).where(
                IngestState.inbox_id == alpha.id,
                IngestState.epoch == "0.git",
            )
        ).scalar_one()
    assert state.last_commit_sha == head


def test_ingest_resume_skips_already_walked_commits(seeded_db, tmp_path):
    """A second ingest_epoch without rewinding state walks zero
    commits: the dulwich excludes-set covers HEAD."""
    alpha = _alpha(seeded_db)
    repo_path = tmp_path / "0.git"
    _build_pubinbox_repo(repo_path, [_rfc5322(f"r{i}@example.com") for i in range(2)])

    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", repo_path, workers=1)
        second = ingest_epoch(s, alpha, "0.git", repo_path, workers=1)

    assert second.new == 0
    assert second.linked == 0
    assert second.dup_batch == 0
    assert second.dup_db == 0
    assert second.failed == 0


# Persisted parse failures


def test_failed_parse_persists_parse_failures_row(seeded_db, tmp_path):
    """A commit whose blob can't be parsed gets a row in
    parse_failures keyed by (inbox, epoch, commit_sha)."""
    alpha = _alpha(seeded_db)
    bad = b"From: a@b.example\r\nSubject: no msgid\r\n\r\nbody"
    _build_pubinbox_repo(tmp_path / "0.git", [bad])

    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    head = Repo(str(tmp_path / "0.git")).head().decode()
    with seeded_db() as s:
        rows = list(s.execute(
            select(ParseFailure).where(ParseFailure.inbox_id == alpha.id)
        ).scalars())
    assert len(rows) == 1
    row = rows[0]
    assert row.commit_sha == head
    assert row.epoch == "0.git"
    assert row.attempts == 1
    assert row.error_class  # whatever parser raises — class name pinned
    assert row.first_seen == row.last_attempt


def test_failed_parse_re_walked_increments_attempts(seeded_db, tmp_path):
    """A second walk over the same bad commit bumps `attempts` and
    `last_attempt`, leaves `first_seen` alone."""
    alpha = _alpha(seeded_db)
    bad = b"From: a@b.example\r\nSubject: no msgid\r\n\r\nbody"
    repo_path = tmp_path / "0.git"
    _build_pubinbox_repo(repo_path, [bad])

    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", repo_path, workers=1)

    with seeded_db() as s:
        first = s.execute(
            select(ParseFailure).where(ParseFailure.inbox_id == alpha.id)
        ).scalar_one()
        first_seen = first.first_seen
        first_last = first.last_attempt

        # Rewind so the walker re-emits the same commit.
        state = s.execute(
            select(IngestState).where(IngestState.inbox_id == alpha.id)
        ).scalar_one()
        state.last_commit_sha = None
        s.commit()

    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", repo_path, workers=1)

    with seeded_db() as s:
        row = s.execute(
            select(ParseFailure).where(ParseFailure.inbox_id == alpha.id)
        ).scalar_one()
    assert row.attempts == 2
    assert row.first_seen == first_seen
    assert row.last_attempt >= first_last


def test_failed_then_succeeds_clears_parse_failures_row(seeded_db, tmp_path, monkeypatch):
    """A commit that failed under an old (artificially-tightened)
    parser parses cleanly after the constraint is relaxed: the
    parse_failures row is deleted on the next walk."""
    import mimir.parser

    alpha = _alpha(seeded_db)
    repo_path = tmp_path / "0.git"
    _build_pubinbox_repo(repo_path, [_rfc5322("recover@example.com", body=b"x" * 500)])

    # First walk: tiny size cap → fail.
    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 200)
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", repo_path, workers=1)
    with seeded_db() as s:
        assert s.execute(
            select(func.count()).select_from(ParseFailure)
            .where(ParseFailure.inbox_id == alpha.id)
        ).scalar_one() == 1

    # Lift the cap, rewind, re-walk: row gets cleared.
    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 50_000_000)
    with seeded_db() as s:
        state = s.execute(
            select(IngestState).where(IngestState.inbox_id == alpha.id)
        ).scalar_one()
        state.last_commit_sha = None
        s.commit()
    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", repo_path, workers=1)
    assert result.new == 1

    with seeded_db() as s:
        assert s.execute(
            select(func.count()).select_from(ParseFailure)
            .where(ParseFailure.inbox_id == alpha.id)
        ).scalar_one() == 0


# replay_failures


def test_replay_failures_recovers_on_parser_fix(seeded_db, tmp_path, monkeypatch):
    """Ingest with a tight cap -> failure rows. Lift the cap, point
    the inbox's mirror_path at the test repo, replay -> rows cleared
    and articles inserted."""
    import mimir.parser

    alpha = _alpha(seeded_db)
    mirror_root = tmp_path / "alpha-mirror"
    mirror_root.mkdir()
    _build_pubinbox_repo(mirror_root / "0.git", [
        _rfc5322("a@example.com", body=b"x" * 500),
        _rfc5322("b@example.com", body=b"x" * 500),
    ])

    # Update the seeded inbox row so replay_failures can find the repo.
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(mirror_root)
        s.commit()

    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 200)
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", mirror_root / "0.git", workers=1)

    with seeded_db() as s:
        assert s.execute(
            select(func.count()).select_from(ParseFailure)
            .where(ParseFailure.inbox_id == alpha.id)
        ).scalar_one() == 2

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
        assert s.execute(
            select(func.count()).select_from(ParseFailure)
            .where(ParseFailure.inbox_id == alpha.id)
        ).scalar_one() == 0
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


def test_replay_failures_still_fails_bumps_attempts(seeded_db, tmp_path, monkeypatch):
    """Replay against an unfixed parser: the row's attempts/last_attempt
    advance but the row stays."""
    import mimir.parser

    alpha = _alpha(seeded_db)
    mirror_root = tmp_path / "alpha-mirror"
    mirror_root.mkdir()
    _build_pubinbox_repo(mirror_root / "0.git", [
        _rfc5322("c@example.com", body=b"x" * 500),
    ])
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


def test_replay_failures_skips_when_mirror_missing(seeded_db, tmp_path, monkeypatch):
    """If the mirror has been wiped (or the row predates the current
    mirror layout), replay reports `skipped` and leaves the row."""
    import mimir.parser

    alpha = _alpha(seeded_db)
    # Set up + seed a failure row, then point mirror_path elsewhere.
    mirror_root = tmp_path / "alpha-mirror"
    mirror_root.mkdir()
    _build_pubinbox_repo(mirror_root / "0.git", [
        _rfc5322("d@example.com", body=b"x" * 500),
    ])
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
        assert s.execute(
            select(func.count()).select_from(ParseFailure)
            .where(ParseFailure.inbox_id == alpha.id)
        ).scalar_one() == 1


# Auto-ANALYZE on threshold-crossing ingest


def _setup_alpha_with_messages(seeded_db, tmp_path, n: int) -> Inbox:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _build_pubinbox_repo(
        mirror / "0.git",
        [_rfc5322(f"auto{i}@example.com") for i in range(n)],
    )
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ix.mirror_path = str(mirror)
        s.commit()
        return s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()


def _spy_text(monkeypatch) -> list[str]:
    from mimir import ingest as ingest_mod
    seen: list[str] = []
    real_text = ingest_mod.text
    def _spy(stmt):
        seen.append(stmt)
        return real_text(stmt)
    monkeypatch.setattr(ingest_mod, "text", _spy)
    return seen


def test_ingest_inbox_runs_analyze_when_threshold_reached(seeded_db, tmp_path, monkeypatch):
    from mimir.config import settings

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    seen = _spy_text(monkeypatch)
    monkeypatch.setattr(settings, "analyze_after_ingest_rows", 2)

    results = ingest_inbox(alpha, workers=1)

    assert sum(r.new + r.linked for r in results) >= 2
    assert "ANALYZE" in seen


def test_ingest_inbox_skips_analyze_below_threshold(seeded_db, tmp_path, monkeypatch):
    from mimir.config import settings

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    seen = _spy_text(monkeypatch)
    monkeypatch.setattr(settings, "analyze_after_ingest_rows", 100)

    ingest_inbox(alpha, workers=1)

    assert "ANALYZE" not in seen


def test_ingest_inbox_skips_analyze_when_disabled(seeded_db, tmp_path, monkeypatch):
    from mimir.config import settings

    alpha = _setup_alpha_with_messages(seeded_db, tmp_path, 3)
    seen = _spy_text(monkeypatch)
    monkeypatch.setattr(settings, "analyze_after_ingest_rows", 0)

    ingest_inbox(alpha, workers=1)

    assert "ANALYZE" not in seen


# Canonical inbox + list-address observation


def test_ingest_records_list_address_observations(seeded_db, tmp_path):
    """Each list-shaped To/Cc address surfaces as a row in
    inbox_address_observations, scoped to the ingesting inbox."""
    alpha = _alpha(seeded_db)
    raw = _rfc5322(
        "obs1@example.com",
        to="linux-fsdevel@vger.kernel.org",
        cc="linux-kernel@vger.kernel.org, alice@example.com",
    )
    _build_pubinbox_repo(tmp_path / "0.git", [raw])

    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    with seeded_db() as s:
        rows = s.execute(
            select(InboxAddressObservation.address, InboxAddressObservation.count)
            .where(InboxAddressObservation.inbox_id == alpha.id)
        ).all()
    addresses = {addr: cnt for addr, cnt in rows}
    # alice@example.com is filtered out (not list-shaped); the two
    # list addresses are recorded with count=1 each.
    assert addresses == {
        "linux-fsdevel@vger.kernel.org": 1,
        "linux-kernel@vger.kernel.org": 1,
    }


def test_ingest_observations_accumulate_across_messages(seeded_db, tmp_path):
    alpha = _alpha(seeded_db)
    msgs = [
        _rfc5322(f"acc{i}@example.com", to="linux-fsdevel@vger.kernel.org")
        for i in range(5)
    ]
    _build_pubinbox_repo(tmp_path / "0.git", msgs)

    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    with seeded_db() as s:
        cnt = s.execute(
            select(InboxAddressObservation.count)
            .where(InboxAddressObservation.inbox_id == alpha.id)
            .where(InboxAddressObservation.address == "linux-fsdevel@vger.kernel.org")
        ).scalar_one()
    assert cnt == 5


def test_ingest_sets_canonical_when_to_address_matches_known_inbox(seeded_db, tmp_path):
    """alpha already has list_address set; ingesting a message into
    beta whose To: points at alpha sets canonical_inbox_id=alpha.id."""
    with seeded_db() as s:
        a = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        a.list_address = "linux-fsdevel@vger.kernel.org"
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()

    raw = _rfc5322(
        "canon1@example.com",
        to="linux-fsdevel@vger.kernel.org",
        cc="linux-kernel@vger.kernel.org",
    )
    _build_pubinbox_repo(tmp_path / "0.git", [raw])

    with seeded_db() as s:
        ingest_epoch(s, beta, "0.git", tmp_path / "0.git", workers=1)

    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "canon1@example.com")
        ).scalar_one()
        assert art.canonical_inbox_id == alpha.id


def test_ingest_canonical_null_when_no_known_address_matches(seeded_db, tmp_path):
    alpha = _alpha(seeded_db)
    raw = _rfc5322(
        "canon-null@example.com",
        to="linux-mm@kvack.org",  # list-shaped but no inbox has this list_address
    )
    _build_pubinbox_repo(tmp_path / "0.git", [raw])

    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "canon-null@example.com")
        ).scalar_one()
        assert art.canonical_inbox_id is None


def test_promote_list_address_below_threshold_skips(seeded_db):
    """Below MIN_PROMOTE_OBSERVATIONS samples, promotion stays its
    hand — even with a clear modal address."""
    alpha = _alpha(seeded_db)
    with seeded_db() as s:
        s.add(InboxAddressObservation(
            inbox_id=alpha.id,
            address="linux-fsdevel@vger.kernel.org",
            count=MIN_PROMOTE_OBSERVATIONS - 1,
            last_seen=datetime(2024, 1, 1),
        ))
        s.commit()
        result = _maybe_promote_list_address(s, alpha.id)
        s.commit()
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        assert result is None
        assert ix.list_address is None


def test_promote_list_address_clear_modal_promotes(seeded_db):
    alpha = _alpha(seeded_db)
    with seeded_db() as s:
        s.add(InboxAddressObservation(
            inbox_id=alpha.id,
            address="linux-fsdevel@vger.kernel.org",
            count=200,
            last_seen=datetime(2024, 1, 1),
        ))
        s.add(InboxAddressObservation(
            inbox_id=alpha.id,
            address="linux-kernel@vger.kernel.org",
            count=20,  # 200/(200+20) = 0.91 dominance, easily above 0.7
            last_seen=datetime(2024, 1, 1),
        ))
        s.commit()
        result = _maybe_promote_list_address(s, alpha.id)
        s.commit()
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        assert result == "linux-fsdevel@vger.kernel.org"
        assert ix.list_address == "linux-fsdevel@vger.kernel.org"


def test_promote_list_address_split_decision_skips(seeded_db):
    """Two roughly-tied addresses: dominance < 0.7 keeps promotion off
    so we don't lock in the wrong canonical."""
    alpha = _alpha(seeded_db)
    with seeded_db() as s:
        s.add(InboxAddressObservation(
            inbox_id=alpha.id,
            address="linux-fsdevel@vger.kernel.org",
            count=100,
            last_seen=datetime(2024, 1, 1),
        ))
        s.add(InboxAddressObservation(
            inbox_id=alpha.id,
            address="linux-kernel@vger.kernel.org",
            count=80,  # 100/180 = 0.55, below 0.7 dominance
            last_seen=datetime(2024, 1, 1),
        ))
        s.commit()
        result = _maybe_promote_list_address(s, alpha.id)
        s.commit()
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        assert result is None
        assert ix.list_address is None


def test_promote_list_address_already_set_no_overwrite(seeded_db):
    alpha = _alpha(seeded_db)
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.list_address = "operator-override@example.com"
        s.add(InboxAddressObservation(
            inbox_id=alpha.id,
            address="linux-fsdevel@vger.kernel.org",
            count=10000,
            last_seen=datetime(2024, 1, 1),
        ))
        s.commit()
        result = _maybe_promote_list_address(s, alpha.id)
        s.commit()
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        assert result is None
        assert ix.list_address == "operator-override@example.com"


# Backfill canonical-inbox + observations from historical blobs


from mimir.ingest import backfill_canonicals  # noqa: E402


def _clear_seed_articles(seeded_db) -> None:
    """Drop the conftest-seeded articles + observations so backfill
    tests only see what THIS test inserts. Inboxes (alpha, beta) stay
    so we still have IDs and a place to set list_address."""
    with seeded_db() as s:
        s.execute(delete(InboxAddressObservation))
        s.execute(delete(ArticleList))
        s.execute(delete(Article))
        s.commit()


def _ingest_with_to(
    seeded_db, tmp_path, inbox: Inbox, msgid: str,
    to: str | None = None, cc: str | None = None,
    repo_dir: str = "0.git",
) -> None:
    """Ingest a single message with optional To/Cc into `inbox`."""
    raw = _rfc5322(msgid, to=to, cc=cc)
    repo_path = tmp_path / repo_dir
    if not repo_path.exists():
        _build_pubinbox_repo(repo_path, [raw])
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == inbox.id)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, repo_dir, repo_path, workers=1)


def test_backfill_resolves_canonical_when_to_matches_known_address(seeded_db, tmp_path):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    _ingest_with_to(
        seeded_db, tmp_path, alpha, "bf1@example.com",
        to="linux-fsdevel@vger.kernel.org",
    )
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "bf1@example.com")
        ).scalar_one()
        assert art.canonical_inbox_id is None
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.list_address = "linux-fsdevel@vger.kernel.org"
        s.commit()

    result = backfill_canonicals(promote_every=1)

    assert result.examined == 1
    assert result.resolved == 1
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "bf1@example.com")
        ).scalar_one()
        assert art.canonical_inbox_id == alpha.id


def test_backfill_unresolved_when_no_address_matches(seeded_db, tmp_path):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    _ingest_with_to(
        seeded_db, tmp_path, alpha, "bf-noop@example.com",
        to="alice@example.com",
    )

    result = backfill_canonicals(promote_every=1)

    assert result.examined == 1
    assert result.resolved == 0
    assert result.unresolved == 1
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "bf-noop@example.com")
        ).scalar_one()
        assert art.canonical_inbox_id is None


def test_backfill_respects_limit(seeded_db, tmp_path):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    msgs = [
        _rfc5322(f"lim{i}@example.com", to="linux-fsdevel@vger.kernel.org")
        for i in range(5)
    ]
    _build_pubinbox_repo(tmp_path / "0.git", msgs)
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, "0.git", tmp_path / "0.git", workers=1)

    result = backfill_canonicals(limit=2)
    assert result.examined == 2


def test_backfill_inbox_filter_restricts_walk(seeded_db, tmp_path):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    _ingest_with_to(
        seeded_db, tmp_path, alpha, "bf-filter@example.com",
        to="linux-fsdevel@vger.kernel.org",
    )

    result = backfill_canonicals(inbox_filter="beta")
    assert result.examined == 0


def test_backfill_skip_when_blob_missing(seeded_db, tmp_path):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    _ingest_with_to(
        seeded_db, tmp_path, alpha, "bf-gone@example.com",
        to="linux-fsdevel@vger.kernel.org",
    )

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(tmp_path / "vanished")
        s.commit()

    result = backfill_canonicals(promote_every=1)
    assert result.examined == 1
    assert result.skipped == 1
    assert result.resolved == 0


def test_backfill_reprocess_re_examines_already_set(seeded_db, tmp_path):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    _ingest_with_to(
        seeded_db, tmp_path, alpha, "bf-rep@example.com",
        to="linux-fsdevel@vger.kernel.org",
    )

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.list_address = "linux-fsdevel@vger.kernel.org"
        s.commit()

    backfill_canonicals(promote_every=1)
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "bf-rep@example.com")
        ).scalar_one()
        assert art.canonical_inbox_id == alpha.id

    with seeded_db() as s:
        a = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        a.list_address = None
        b = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        b.list_address = "linux-fsdevel@vger.kernel.org"
        s.commit()
        beta_id = b.id

    no_op = backfill_canonicals(promote_every=1)
    assert no_op.examined == 0

    rep = backfill_canonicals(reprocess=True, promote_every=1)
    assert rep.examined == 1
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "bf-rep@example.com")
        ).scalar_one()
        assert art.canonical_inbox_id == beta_id


def test_backfill_records_observations(seeded_db, tmp_path):
    _clear_seed_articles(seeded_db)
    alpha = _alpha(seeded_db)
    msgs = [
        _rfc5322(f"obs{i}@example.com", to="linux-fsdevel@vger.kernel.org")
        for i in range(3)
    ]
    _build_pubinbox_repo(tmp_path / "0.git", msgs)
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(tmp_path)
        s.commit()
        ingest_epoch(s, ix, "0.git", tmp_path / "0.git", workers=1)

    with seeded_db() as s:
        s.execute(delete(InboxAddressObservation))
        s.commit()

    backfill_canonicals(promote_every=10, reprocess=True)

    with seeded_db() as s:
        cnt = s.execute(
            select(InboxAddressObservation.count)
            .where(InboxAddressObservation.inbox_id == alpha.id)
            .where(InboxAddressObservation.address == "linux-fsdevel@vger.kernel.org")
        ).scalar_one()
    assert cnt == 3
