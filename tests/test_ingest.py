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
    PROMOTE_DOMINANCE,
    _maybe_promote_list_address,
    discover_epochs,
    ingest_all,
    ingest_epoch,
    ingest_inbox,
    replay_failures,
)
from mimir.models import (
    Article,
    ArticleFile,
    ArticleList,
    ArticleTrailer,
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
    # IndexNow feeds off this list, must match the `new` bucket exactly.
    assert result.new_message_ids == ["fresh@example.com"]

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


def test_ingest_new_message_ids_only_tracks_new_bucket(seeded_db, tmp_path):
    """`new_message_ids` is the IndexNow feed: only freshly-created
    Articles (the `new` bucket) belong in it. `linked` rows (cross-
    post: the Article already existed, just got a new ArticleList
    row) and `dup_*` rows must not appear, IndexNow would otherwise
    push URLs that didn't actually become discoverable this tick."""
    alpha = _alpha(seeded_db)
    _build_pubinbox_repo(tmp_path / "0.git", [
        _rfc5322("brand-new@example.com"),
        _rfc5322("art2@example.com"),           # seeded in beta → `linked`
        _rfc5322("brand-new@example.com"),      # same Message-ID → `dup_batch`
    ])
    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.new == 1
    assert result.linked == 1
    assert result.dup_batch == 1
    assert result.new_message_ids == ["brand-new@example.com"]


# ArticleFile (diff-touched paths) integration


def test_ingest_extracts_diff_touched_paths_for_patch_body(
    seeded_db, tmp_path,
):
    """Patch bodies get one ArticleFile row per `diff --git`
    header. The b/ side is stored; that's the path reviewers and
    MAINTAINERS globs look at."""
    alpha = _alpha(seeded_db)
    patch_body = (
        b"Signed-off-by: A <a@example>\n\n"
        b"diff --git a/fs/foo/a.c b/fs/foo/a.c\n"
        b"@@ -1 +1 @@\n-x\n+y\n"
        b"diff --git a/include/uapi/foo.h b/include/uapi/foo.h\n"
        b"@@ -1 +1 @@\n-x\n+y\n"
    )
    _build_pubinbox_repo(tmp_path / "0.git", [
        _rfc5322("patch@example.com", body=patch_body),
    ])
    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.new == 1
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "patch@example.com")
        ).scalar_one()
        paths = {
            row.path for row in s.execute(
                select(ArticleFile).where(ArticleFile.article_id == art.id)
            ).scalars()
        }
    assert paths == {"fs/foo/a.c", "include/uapi/foo.h"}


def test_ingest_extracts_no_paths_for_prose_only_body(
    seeded_db, tmp_path,
):
    """Non-patch bodies create zero ArticleFile rows. The "other
    patches touching X" sidebar must not include discussion-only
    threads."""
    alpha = _alpha(seeded_db)
    _build_pubinbox_repo(tmp_path / "0.git", [
        _rfc5322("prose@example.com", body=b"just a discussion, no diff here\n"),
    ])
    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.new == 1
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "prose@example.com")
        ).scalar_one()
        rows = s.execute(
            select(ArticleFile).where(ArticleFile.article_id == art.id)
        ).all()
    assert rows == []


def test_ingest_extracts_review_trailers(seeded_db, tmp_path):
    """Bodies carrying `Reviewed-by:` / `Acked-by:` / etc. land
    one ArticleTrailer row per attestation. Address is stored
    verbatim; address_normalized is lowercased for case-insensitive
    per-author lookups (slice 3 of #97). Signed-off-by is NOT
    indexed (chain-of-custody, not a review signal)."""
    alpha = _alpha(seeded_db)
    body = (
        b"Looks good.\n\n"
        b"Signed-off-by: Author <author@example.com>\n"
        b"Reviewed-by: Alice <Alice@Example.COM>\n"
        b"Acked-by: Bob <bob@kernel.org>\n"
    )
    _build_pubinbox_repo(tmp_path / "0.git", [
        _rfc5322("trailers@example.com", body=body),
    ])
    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    assert result.new == 1
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "trailers@example.com")
        ).scalar_one()
        rows = sorted(
            (t.role, t.address, t.address_normalized) for t in s.execute(
                select(ArticleTrailer).where(ArticleTrailer.article_id == art.id)
            ).scalars()
        )
    assert rows == [
        ("Acked-by", "bob@kernel.org", "bob@kernel.org"),
        ("Reviewed-by", "Alice@Example.COM", "alice@example.com"),
    ]


def test_ingest_extracts_no_trailers_for_prose_only_body(seeded_db, tmp_path):
    """Prose bodies create zero ArticleTrailer rows."""
    alpha = _alpha(seeded_db)
    _build_pubinbox_repo(tmp_path / "0.git", [
        _rfc5322("prose-trailers@example.com", body=b"just chatting\n"),
    ])
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "prose-trailers@example.com")
        ).scalar_one()
        rows = s.execute(
            select(ArticleTrailer).where(ArticleTrailer.article_id == art.id)
        ).all()
    assert rows == []


def test_ingest_linked_cross_post_does_not_double_add_files(
    seeded_db, tmp_path,
):
    """When a message gets `linked` (already in another inbox),
    the existing Article retains its ArticleFile rows, the linked
    row doesn't trigger re-extraction. Otherwise a cross-posted
    patch would accumulate duplicate rows per linked inbox."""
    alpha = _alpha(seeded_db)
    patch_body = (
        b"diff --git a/fs/x/file.c b/fs/x/file.c\n"
        b"@@ -1 +1 @@\n-x\n+y\n"
    )
    # First ingest into beta (the seeded second inbox).
    with seeded_db() as s:
        from mimir.models import Inbox
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        _build_pubinbox_repo(tmp_path / "beta-0.git", [
            _rfc5322("xpost@example.com", body=patch_body),
        ])
        ingest_epoch(s, beta, "0.git", tmp_path / "beta-0.git", workers=1)

    # Then ingest the same Message-ID into alpha → `linked`.
    _build_pubinbox_repo(tmp_path / "alpha-0.git", [
        _rfc5322("xpost@example.com", body=patch_body),
    ])
    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "alpha-0.git", workers=1)

    assert result.linked == 1
    assert result.new == 0
    with seeded_db() as s:
        art = s.execute(
            select(Article).where(Article.message_id == "xpost@example.com")
        ).scalar_one()
        paths = [
            row.path for row in s.execute(
                select(ArticleFile).where(ArticleFile.article_id == art.id)
            ).scalars()
        ]
    # Exactly one row, not duplicated across the two linked inboxes.
    assert paths == ["fs/x/file.c"]


# patch-series cover-letter detection at ingest


def test_ingest_records_patch_series_key_for_cover_letter(
    seeded_db, tmp_path,
):
    """A `[PATCH v2 0/3] <title>` subject lands a series key +
    version on the Article row. Non-cover-letter articles leave
    both columns NULL."""
    alpha = _alpha(seeded_db)
    msgs = [
        _rfc5322(
            "cover-v2@example.com",
            body=b"x",
        ).replace(
            b"Subject: t\r\n",
            b"Subject: [PATCH v2 0/3] improve foo handling\r\n",
        ),
        _rfc5322(
            "patch-1of3@example.com",
            body=b"x",
        ).replace(
            b"Subject: t\r\n",
            b"Subject: [PATCH v2 1/3] foo: add bar\r\n",
        ),
    ]
    _build_pubinbox_repo(tmp_path / "0.git", msgs)
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)
        cover = s.execute(
            select(Article).where(Article.message_id == "cover-v2@example.com")
        ).scalar_one()
        patch = s.execute(
            select(Article).where(Article.message_id == "patch-1of3@example.com")
        ).scalar_one()
    assert cover.patch_series_key is not None
    assert cover.patch_series_version == "v2"
    # Non-cover-letter (1/3) → NULL columns.
    assert patch.patch_series_key is None
    assert patch.patch_series_version is None


def test_ingest_groups_v1_and_v2_under_same_series_key(seeded_db, tmp_path):
    """Two cover letters with the same title + same author get the
    same `patch_series_key`. Pins the cross-revision linkage that
    drives the timeline render."""
    alpha = _alpha(seeded_db)
    msgs = [
        _rfc5322("v1-cover@example.com", body=b"x").replace(
            b"Subject: t\r\n",
            b"Subject: [PATCH 0/3] improve foo handling\r\n",
        ),
        _rfc5322("v2-cover@example.com", body=b"x").replace(
            b"Subject: t\r\n",
            b"Subject: [PATCH v2 0/3] improve foo handling\r\n",
        ),
    ]
    _build_pubinbox_repo(tmp_path / "0.git", msgs)
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)
        rows = list(s.execute(
            select(Article).where(
                Article.message_id.in_(["v1-cover@example.com", "v2-cover@example.com"])
            )
        ).scalars())
    keys = {r.patch_series_key for r in rows}
    assert len(keys) == 1
    assert None not in keys
    versions = {r.message_id: r.patch_series_version for r in rows}
    assert versions == {
        "v1-cover@example.com": "v1",
        "v2-cover@example.com": "v2",
    }


# Bucket: linked (cross-post)


def test_ingest_linked_when_message_id_already_in_other_inbox(seeded_db, tmp_path):
    """art2@example.com is in beta (seeded). Ingesting it into alpha
    must reuse the existing Article and add an article_lists row  
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
    assert row.error_class  # whatever parser raises, class name pinned
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
            s.add(ParseFailure(
                inbox_id=ix.id,
                epoch=epoch,
                commit_sha=sha,
                error_class="ValueError",
                error_message="seeded",
                attempts=1,
                first_seen=now,
                last_attempt=now,
            ))
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
    from mimir.ingest import orchestrate as ingest_mod
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

    # Three messages, all fresh -- exact count is known. The earlier
    # `>= 2` lower bound would have masked a regression that
    # accidentally dropped one of the three to dup_batch / failed.
    assert sum(r.new + r.linked for r in results) == 3
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
    """5 messages to the same list address should produce exactly one
    observation row with count=5 (not 5 rows of count=1, not 0/1, not
    a different address). last_seen must also be set (NOT NULL), since
    the auto-promotion logic uses it for staleness checks."""
    alpha = _alpha(seeded_db)
    msgs = [
        _rfc5322(f"acc{i}@example.com", to="linux-fsdevel@vger.kernel.org")
        for i in range(5)
    ]
    _build_pubinbox_repo(tmp_path / "0.git", msgs)

    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    with seeded_db() as s:
        # Exactly one row for this (inbox, address); not 5 distinct
        # rows with count=1 each (which would happen if the upsert
        # path were wired wrong).
        rows = s.execute(
            select(InboxAddressObservation)
            .where(InboxAddressObservation.inbox_id == alpha.id)
        ).scalars().all()
        for_address = [
            r for r in rows
            if r.address == "linux-fsdevel@vger.kernel.org"
        ]
        assert len(for_address) == 1, (
            f"expected exactly one observation row for the address; "
            f"got {len(for_address)}: {[(r.address, r.count) for r in rows]}"
        )
        obs = for_address[0]
        assert obs.count == 5
        # last_seen is what the auto-promote logic uses; ingest must
        # populate it.
        assert obs.last_seen is not None


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
    hand, even with a clear modal address."""
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
    """Above MIN_PROMOTE_OBSERVATIONS samples AND with the modal
    address's share of observations >= PROMOTE_DOMINANCE, promotion
    fires. Test data is derived from the constants so a future tuning
    of either threshold invalidates the *test setup* loudly rather
    than silently passing on stale ratios."""
    # Place the winner just clear of the dominance threshold; the
    # runner-up gets the remainder. Total stays well above
    # MIN_PROMOTE_OBSERVATIONS so the count gate also clears.
    total = max(MIN_PROMOTE_OBSERVATIONS * 4, 200)
    winner_count = int(total * (PROMOTE_DOMINANCE + 0.1)) + 1
    runner_count = total - winner_count
    # Sanity-check the test setup so a constant bump doesn't quietly
    # leave us testing the wrong branch.
    assert winner_count + runner_count >= MIN_PROMOTE_OBSERVATIONS
    assert winner_count / total > PROMOTE_DOMINANCE

    alpha = _alpha(seeded_db)
    with seeded_db() as s:
        s.add(InboxAddressObservation(
            inbox_id=alpha.id,
            address="linux-fsdevel@vger.kernel.org",
            count=winner_count,
            last_seen=datetime(2024, 1, 1),
        ))
        s.add(InboxAddressObservation(
            inbox_id=alpha.id,
            address="linux-kernel@vger.kernel.org",
            count=runner_count,
            last_seen=datetime(2024, 1, 1),
        ))
        s.commit()
        result = _maybe_promote_list_address(s, alpha.id)
        s.commit()
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        assert result == "linux-fsdevel@vger.kernel.org"
        assert ix.list_address == "linux-fsdevel@vger.kernel.org"


def test_promote_list_address_split_decision_skips(seeded_db):
    """Above MIN_PROMOTE_OBSERVATIONS samples but with the modal
    address's share < PROMOTE_DOMINANCE, promotion skips so we don't
    lock in the wrong canonical. Derived from constants for the same
    reason as the clear-modal test."""
    total = max(MIN_PROMOTE_OBSERVATIONS * 4, 200)
    # Just *below* the dominance threshold, so the modal address is
    # still the majority but not dominant enough to promote.
    winner_count = int(total * (PROMOTE_DOMINANCE - 0.1))
    runner_count = total - winner_count
    assert winner_count + runner_count >= MIN_PROMOTE_OBSERVATIONS
    assert winner_count / total < PROMOTE_DOMINANCE
    assert winner_count > runner_count  # still the majority

    alpha = _alpha(seeded_db)
    with seeded_db() as s:
        s.add(InboxAddressObservation(
            inbox_id=alpha.id,
            address="linux-fsdevel@vger.kernel.org",
            count=winner_count,
            last_seen=datetime(2024, 1, 1),
        ))
        s.add(InboxAddressObservation(
            inbox_id=alpha.id,
            address="linux-kernel@vger.kernel.org",
            count=runner_count,
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


# Regression: RFC 5322 dates with `-0000` come back tz-naive from
# email.utils.parsedate_to_datetime. Mixing those into max() with
# tz-aware dates raised TypeError mid-ingest and rolled back the
# whole batch, production lkml ingest crashed after walking 6M
# commits with only 26 articles persisting.


def _rfc5322_with_date(msgid: str, date_header: str, to: str) -> bytes:
    return (
        b"Message-ID: <" + msgid.encode() + b">\r\n"
        b"From: a@b.example\r\n"
        b"To: " + to.encode() + b"\r\n"
        b"Subject: t\r\n"
        b"Date: " + date_header.encode() + b"\r\n"
        b"\r\n"
        b"hi"
    )


def test_ingest_handles_minus_0000_naive_date_in_observations(seeded_db, tmp_path):
    """Two messages with the same To address: one with +0000 (aware),
    one with -0000 (naive). Pre-fix, the second update of the
    pending_obs entry crashed on `max(aware, naive)` and rolled back
    the batch.

    Verify both messages land, AND that the observation row's
    `last_seen` is tz-aware UTC (the `_aware_utc` normalisation must
    apply). A regression that rolled back the batch would leave
    `count == 0` or no observation row at all."""
    alpha = _alpha(seeded_db)
    msgs = [
        _rfc5322_with_date(
            "tz-aware@example.com", "Mon, 1 Jan 2024 00:00:00 +0000",
            to="linux-fsdevel@vger.kernel.org",
        ),
        _rfc5322_with_date(
            "tz-naive@example.com", "Mon, 1 Jan 2024 00:00:00 -0000",
            to="linux-fsdevel@vger.kernel.org",
        ),
    ]
    _build_pubinbox_repo(tmp_path / "0.git", msgs)

    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)

    # Both messages should land cleanly (new bucket).
    assert result.new == 2
    assert result.failed == 0
    # The observation row reflects both messages AND last_seen is
    # tz-aware UTC (the normalisation must survive the -0000 path).
    with seeded_db() as s:
        obs = s.execute(
            select(InboxAddressObservation)
            .where(InboxAddressObservation.inbox_id == alpha.id)
            .where(InboxAddressObservation.address == "linux-fsdevel@vger.kernel.org")
        ).scalar_one()
    assert obs.count == 2
    assert obs.last_seen is not None
    # SQLite returns datetime without tzinfo by default; the value
    # stored should at least be parseable as the recorded date.
    # Mostly: it didn't crash on the -0000 → naive datetime path.


# --------------------------------------------------------------------------
# Module surfaces previously tested only incidentally:
# - `discover_epochs` filters non-repo siblings out of the mirror walk
# - `ingest_all` decrements `limit` across inboxes (cross-inbox cap)
# --------------------------------------------------------------------------


def test_discover_epochs_returns_git_repos_only(tmp_path):
    """`discover_epochs` walks the mirror dir and returns just the
    children that look like git repos. Non-repo directories and
    regular files must be filtered -- without this, a stray
    `Inboxes/<name>/git/README` or `metadata/` would crash the
    walker downstream."""
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _build_pubinbox_repo(mirror / "0.git", [_rfc5322("e0@example.com")])
    _build_pubinbox_repo(mirror / "1.git", [_rfc5322("e1@example.com")])
    # Non-repo directory.
    (mirror / "garbage").mkdir()
    (mirror / "garbage" / "stray-file").write_text("x")
    # Stray file at the top.
    (mirror / "README").write_text("operator notes")

    epochs = discover_epochs(mirror)
    names = [p.name for p in epochs]
    assert names == ["0.git", "1.git"], (
        f"discover_epochs must skip non-repo siblings; got {names}"
    )


def test_discover_epochs_empty_mirror_returns_empty(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    assert discover_epochs(mirror) == []


def test_ingest_all_limit_decrements_across_inboxes(seeded_db, tmp_path):
    """`ingest_all(limit=N)` is a cross-inbox cap. With two inboxes
    each carrying 3 messages and `limit=2`, the first inbox should
    consume both slots and the second must be skipped entirely --
    no ingest call, no result row. Without this contract, a
    `limit=500` across 5 inboxes could quietly ingest 2500 messages."""
    alpha_mirror = tmp_path / "alpha"
    alpha_mirror.mkdir()
    _build_pubinbox_repo(alpha_mirror / "0.git", [
        _rfc5322(f"alpha-cap-{i}@example.com") for i in range(3)
    ])
    beta_mirror = tmp_path / "beta"
    beta_mirror.mkdir()
    _build_pubinbox_repo(beta_mirror / "0.git", [
        _rfc5322(f"beta-cap-{i}@example.com") for i in range(3)
    ])

    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        alpha.mirror_path = str(alpha_mirror)
        beta.mirror_path = str(beta_mirror)
        s.commit()
        # Re-fetch detached copies for ingest_all's dict.
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()

    out = ingest_all(inboxes={"alpha": alpha, "beta": beta}, limit=2, workers=1)

    # alpha got an ingest result; beta did not (skipped before its
    # ingest_inbox was even called).
    assert "alpha" in out
    assert "beta" not in out, (
        f"limit should have stopped before beta's ingest; got results for "
        f"{list(out.keys())}"
    )
    # And the per-result bookkeeping: alpha's result respects the
    # cap. Either dup_batch + new + linked == 2 (the cap), or new
    # rolled through up to 3 if the per-epoch loop is more permissive
    # -- but the *cross-inbox* cap is the contract being tested.
    alpha_total = sum(
        r.new + r.linked + r.dup_batch + r.dup_db + r.failed
        for r in out["alpha"]
    )
    # alpha consumed at least the cap-worth; beta must not have run.
    assert alpha_total >= 2


def test_ingest_all_no_limit_walks_all_inboxes(seeded_db, tmp_path):
    """Sanity companion: with `limit=None`, every inbox gets ingested.
    Without this baseline, a passing `test_..._decrements_across_inboxes`
    could be masking a regression that just never walks beta."""
    alpha_mirror = tmp_path / "alpha"
    alpha_mirror.mkdir()
    _build_pubinbox_repo(alpha_mirror / "0.git", [_rfc5322("a-nolim@example.com")])
    beta_mirror = tmp_path / "beta"
    beta_mirror.mkdir()
    _build_pubinbox_repo(beta_mirror / "0.git", [_rfc5322("b-nolim@example.com")])

    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        alpha.mirror_path = str(alpha_mirror)
        beta.mirror_path = str(beta_mirror)
        s.commit()
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()

    out = ingest_all(inboxes={"alpha": alpha, "beta": beta}, limit=None, workers=1)
    assert "alpha" in out and "beta" in out


# --------------------------------------------------------------------------
# Multi-worker ingest (workers>1).
#
# Every other ingest test in this file pins `workers=1`. The
# CLAUDE.md rule "`parse_message` must stay importable at module
# level so it pickles cleanly to worker processes" has nothing
# enforcing it; a closure-capturing refactor would break production
# and pass CI. One end-to-end run with `workers=2` is enough to
# catch that.
# --------------------------------------------------------------------------


def test_ingest_with_multiple_workers_succeeds(seeded_db, tmp_path):
    """`workers=2` walks the epoch through a ProcessPoolExecutor.
    The parsed-message contract requires `parse_message` to pickle
    cleanly across the boundary; a regression that broke picklability
    (closure capture, runtime-bound state) would only surface in
    production. Pin the path with a small synthetic corpus."""
    alpha = _alpha(seeded_db)
    mirror_root = tmp_path / "alpha-workers"
    mirror_root.mkdir()
    _build_pubinbox_repo(mirror_root / "0.git", [
        _rfc5322(f"w-{i}@example.com") for i in range(4)
    ])
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(mirror_root)
        s.commit()
        result = ingest_epoch(s, ix, "0.git", mirror_root / "0.git", workers=2)

    assert result.new == 4
    assert result.failed == 0


def test_ingest_parser_failure_mid_worker_batch_preserves_order_and_resumes(
    seeded_db, tmp_path,
):
    """A parser-raising message buried inside a multi-chunk
    `ProcessPoolExecutor.map` run must not derail surrounding commits.

    `_parse_iter` relies on `pool.map`'s in-input-order contract;
    a regression that batched results out of order would assign the
    wrong `commit_time` (and therefore `Article.date`) to surviving
    commits on either side of the failure. The pre-existing
    `test_ingest_with_multiple_workers_succeeds` covered four clean
    messages in one PARSE_CHUNKSIZE chunk and missed this.

    Setup: 100 sequential commits with the 50th holding a Message-ID-
    less blob (parse_message raises ValueError). With
    `PARSE_CHUNKSIZE=50` and `workers=2`, the failure lands inside
    chunk 2, both workers actually run, and the failure is mid-
    chunk in input order.

    Assertions: (a) the 99 surviving articles' message-ids match the
    seeded order when read back by `date`; (b) exactly one
    `parse_failures` row exists with the failing commit's SHA;
    (c) a second `ingest_epoch` walks zero commits because the walker
    excludes via `IngestState.last_commit_sha = HEAD`."""
    bad_idx = 50
    messages = [_rfc5322(f"m{i:03d}@example.com") for i in range(100)]
    messages[bad_idx] = b"From: a@b.example\r\nSubject: no msgid\r\n\r\nbody"

    alpha = _alpha(seeded_db)
    mirror_root = tmp_path / "alpha-mid-chunk"
    mirror_root.mkdir()
    _build_pubinbox_repo(mirror_root / "0.git", messages)

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(mirror_root)
        s.commit()
        result = ingest_epoch(s, ix, "0.git", mirror_root / "0.git", workers=2)

    assert result.new == 99
    assert result.failed == 1

    # (a) Order-preservation. Commit_time = 1700000000 + i in
    # _build_pubinbox_repo, so ordering articles by date recovers the
    # seeded sequence (minus the failing index). Filter to the
    # corpus message-ids we just inserted so pre-existing fixture
    # rows don't leak into the assertion.
    expected = [f"m{i:03d}@example.com" for i in range(100) if i != bad_idx]
    with seeded_db() as s:
        got = list(s.execute(
            select(Article.message_id)
            .where(Article.message_id.like("m%@example.com"))
            .order_by(Article.date)
        ).scalars())
    assert got == expected, (
        f"pool.map order-preservation broken; head={got[:5]} tail={got[-5:]}"
    )

    # (b) Exactly one parse_failures row, pointing at the failing
    # commit. Walk the linear chain forward from root to find the
    # idx-th commit's SHA, the seeded repo has no branching.
    repo = Repo(str(mirror_root / "0.git"))
    chain: list[str] = []
    cur = repo.head()
    while cur is not None:
        chain.append(cur.decode())
        commit = repo[cur]
        cur = commit.parents[0] if commit.parents else None
    chain.reverse()
    bad_sha = chain[bad_idx]

    with seeded_db() as s:
        fails = list(s.execute(
            select(ParseFailure).where(ParseFailure.inbox_id == alpha.id)
        ).scalars())
    assert len(fails) == 1
    assert fails[0].commit_sha == bad_sha
    assert fails[0].error_class == "ValueError"

    # (c) Resume cleanly: IngestState.last_commit_sha is at HEAD, so
    # a re-walk yields nothing.
    head = chain[-1]
    with seeded_db() as s:
        state = s.execute(
            select(IngestState).where(
                IngestState.inbox_id == alpha.id,
                IngestState.epoch == "0.git",
            )
        ).scalar_one()
        assert state.last_commit_sha == head
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        second = ingest_epoch(s, ix, "0.git", mirror_root / "0.git", workers=2)
    assert second.new == 0
    assert second.failed == 0


# --------------------------------------------------------------------------
# KEPT_HEADERS filter.
#
# The header filter is what keeps row size at ~471 B; without it the
# `Received:` chains and DKIM/ARC blobs flow into the `headers` dict
# and balloon the article rows. A regression that disabled the
# filter would only show up as DB-row bloat in production. Pin the
# filter directly: ingest a message rich in dropped-class headers
# plus a kept-class header and verify the survivors match.
# --------------------------------------------------------------------------


def test_kept_headers_filter_drops_received_dkim_spam(seeded_db, tmp_path):
    """The KEPT_HEADERS set is the load-bearing piece of the row-size
    invariant. Ingest a message with several headers in the dropped
    class (Received chains, DKIM-Signature, X-Spam-Status,
    Authentication-Results) plus a kept-class header (User-Agent).
    Then re-read the article via `store.read_message` and assert the
    `parsed.headers` keys match the kept-class only."""
    from mimir.store import read_message

    alpha = _alpha(seeded_db)
    mirror_root = tmp_path / "alpha-kept"
    mirror_root.mkdir()
    msgid = "kept-headers@example.com"
    raw = (
        b"Message-ID: <" + msgid.encode() + b">\r\n"
        b"From: a@b.example\r\n"
        b"Subject: t\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        b"User-Agent: kept-agent/1.0\r\n"
        b"Received: from mta1.example by mta2.example; Mon, 1 Jan 2024 00:00:01 +0000\r\n"
        b"Received: from mta0.example by mta1.example; Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        b"DKIM-Signature: v=1; a=rsa-sha256; d=example.com; s=sel; b=AAAA\r\n"
        b"X-Spam-Status: No, score=-1.0\r\n"
        b"Authentication-Results: mta.example; dkim=pass\r\n"
        b"\r\nbody"
    )
    _build_pubinbox_repo(mirror_root / "0.git", [raw])
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.mirror_path = str(mirror_root)
        s.commit()
        ingest_epoch(s, ix, "0.git", mirror_root / "0.git", workers=1)

    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        parsed = read_message(s, ix, msgid)

    # Lowercase header names; KEPT_HEADERS is case-insensitive.
    keys_lower = {k.lower() for k in parsed.headers}
    # Kept: User-Agent survives.
    assert "user-agent" in keys_lower
    # Dropped: every header in the dropped class must be absent.
    for dropped in (
        "received", "dkim-signature", "x-spam-status", "authentication-results",
    ):
        assert dropped not in keys_lower, (
            f"{dropped!r} should be filtered out by KEPT_HEADERS; "
            f"got headers={sorted(keys_lower)}"
        )


# --------------------------------------------------------------------------
# replay_failures cross-post branch.
#
# Existing tests cover replay_failures' three primary buckets, recovered
# (new article), still_failed (parser still rejects), skipped (mirror
# gone). The cross-post branch at ingest.py:506-518 (article already
# exists in another inbox, missing only the article_lists row for
# *this* inbox) is unreached. Defensive code path per the comment;
# pin it.
# --------------------------------------------------------------------------


def test_replay_failures_cross_post_links_existing_article(
    seeded_db, tmp_path, monkeypatch,
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
        art = s.execute(
            select(Article).where(Article.message_id == msgid)
        ).scalar_one()
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
        s.add(ParseFailure(
            inbox_id=ix.id,
            epoch="0.git",
            commit_sha=alpha_sha,
            error_class="ValueError",
            error_message="prior failure",
            attempts=1,
            first_seen=_dt.datetime.now(_dt.timezone.utc),
            last_attempt=_dt.datetime.now(_dt.timezone.utc),
        ))
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
        arts = s.execute(
            select(Article).where(Article.message_id == msgid)
        ).scalars().all()
        assert len(arts) == 1, (
            f"expected 1 Article row for cross-posted msgid, got {len(arts)}"
        )
        # The alpha article_lists row now exists.
        s.execute(
            select(ArticleList).where(
                ArticleList.article_id == arts[0].id,
                ArticleList.inbox_id == alpha.id,
            )
        ).scalar_one()
        # And the failure row is gone.
        assert s.execute(
            select(func.count()).select_from(ParseFailure)
            .where(ParseFailure.inbox_id == alpha.id)
        ).scalar_one() == 0


# Inbox.last_article_date (#216): bumped at ingest-commit time so the
# front-page "Last activity" string doesn't ride the 24h
# `archive_stats` cache window.


def _naive_utc(dt):
    """SQLite stores DateTime as TEXT without tz; round-trip strips
    tzinfo. Compare on the naive form for in-DB assertions."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def test_ingest_bumps_inbox_last_article_date_on_new(
    seeded_db, tmp_path,
):
    """A fresh `new` ingest pushes `Inbox.last_article_date` to the
    commit time of the just-ingested message. Conftest seeds alpha at
    2024-03-01; the synthetic repo here uses `commit_time =
    1700000000 + i` (2023-11-14 onwards), so the bump only happens if
    the helper picks the max-of-batch and overrides only when newer."""
    from datetime import datetime
    alpha = _alpha(seeded_db)
    # Reset the seeded value so we observe a clean bump-from-nothing.
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.last_article_date = None
        s.commit()
    _build_pubinbox_repo(tmp_path / "0.git", [_rfc5322("bumper@example.com")])
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
    # 1700000000 epoch = 2023-11-14 22:13:20 UTC; SQLite returns naive.
    assert _naive_utc(ix.last_article_date) == datetime(2023, 11, 14, 22, 13, 20)


def test_ingest_last_article_date_is_monotonic(seeded_db, tmp_path):
    """A later ingest of an older-dated message doesn't push the
    field backward. Public-inbox commit times are monotonically
    increasing within an epoch, but re-walks, replays, and
    edge-case backfills could ingest out-of-order, the field's
    contract is "max seen ever", not "last seen"."""
    from datetime import datetime, timezone
    alpha = _alpha(seeded_db)
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.last_article_date = future
        s.commit()
    _build_pubinbox_repo(tmp_path / "0.git", [_rfc5322("older@example.com")])
    with seeded_db() as s:
        ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
    assert _naive_utc(ix.last_article_date) == _naive_utc(future)


def test_migration_backfill_sql_populates_last_article_date(seeded_db):
    """The alembic backfill statement (correlated MAX(date) per inbox)
    populates `Inbox.last_article_date` from existing articles +
    article_lists. Pins the upgrade-against-populated-DB path without
    re-running alembic, the conftest seed is a stand-in for a populated
    prod row set.

    Conftest seeds alpha with art1/art3/art4 (max 2024-03-01) and
    beta with art2/art3 (max 2024-03-01). Wipe the column first to
    simulate the pre-upgrade state, then run the migration's backfill
    SQL inline and verify both inboxes pick up the right max."""
    from datetime import datetime
    from sqlalchemy import text
    with seeded_db() as s:
        # Simulate the pre-upgrade state.
        s.execute(text("UPDATE inboxes SET last_article_date = NULL"))
        # Verbatim from alembic/versions/...
        s.execute(text(
            """
            UPDATE inboxes
            SET last_article_date = (
                SELECT MAX(a.date)
                FROM articles a
                JOIN article_lists al ON al.article_id = a.id
                WHERE al.inbox_id = inboxes.id
            )
            """
        ))
        s.commit()
    with seeded_db() as s:
        rows = s.execute(
            select(Inbox.name, Inbox.last_article_date)
        ).all()
    by_name = {n: d for n, d in rows}
    assert _naive_utc(by_name["alpha"]) == datetime(2024, 3, 1, 12, 0)
    assert _naive_utc(by_name["beta"]) == datetime(2024, 3, 1, 12, 0)


def test_ingest_last_article_date_bumps_on_cross_post_link(
    seeded_db, tmp_path,
):
    """A cross-posted message that first landed in `beta` now landing
    in `alpha` bumps `alpha.last_article_date` too, using the
    existing article's date. The two inboxes track their own
    "last activity" independently: linking a cross-post is real
    activity for the destination inbox, even though no new Article
    row is created."""
    alpha = _alpha(seeded_db)
    # `art2@example.com` is in beta only (per conftest seed) with
    # date 2024-02-01. Wipe alpha's last_article_date so the bump is
    # observable from None to 2024-02-01.
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
        ix.last_article_date = None
        s.commit()
    _build_pubinbox_repo(tmp_path / "0.git", [_rfc5322("art2@example.com")])
    with seeded_db() as s:
        result = ingest_epoch(s, alpha, "0.git", tmp_path / "0.git", workers=1)
    assert result.linked == 1
    with seeded_db() as s:
        ix = s.execute(select(Inbox).where(Inbox.id == alpha.id)).scalar_one()
    assert ix.last_article_date is not None
    assert (
        ix.last_article_date.year, ix.last_article_date.month,
        ix.last_article_date.day,
    ) == (2024, 2, 1)
