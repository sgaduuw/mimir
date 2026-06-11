"""Tests for `mimir.mainline`, Link-trailer extraction +
the dulwich-driven commit walker.

Real-world Link trailers (sampled from the upstream Linus tree) are
included as parser fixtures; the walker tests use fake bare repos
built with dulwich so the suite stays offline + fast.
"""

import pytest

from datetime import datetime, timezone
from pathlib import Path

from dulwich.objects import Commit, Tree
from dulwich.repo import Repo
from sqlalchemy import func, select

from mimir.broker.writes import WriterThread
from mimir.mainline import extract_message_ids, walk_commits
from mimir.models import MainlineCommit, MainlineState


@pytest.fixture
def writer_thread():
    """Function-scoped WriterThread for walk_commits tests. Phase 3 of
    the two-pool restructure requires a writer; this fixture starts one
    against the test DB and stops it after the test."""
    wt = WriterThread.from_settings()
    wt.start()
    yield wt
    wt.stop(timeout=10)


# extract_message_ids, pure function, no DB.


def test_extract_short_form_link():
    """`Link: https://lore.kernel.org/r/<msgid>` is the canonical
    short form most commits use."""
    msg = (
        b"Subject line.\n\nLink: https://lore.kernel.org/r/aNU-FkJEcA3T4aDB@intel.com\n"
    )
    assert extract_message_ids(msg) == ["aNU-FkJEcA3T4aDB@intel.com"]


def test_extract_no_slug():
    """Older / hand-crafted commits drop the slug entirely.
    `Link: https://lore.kernel.org/<msgid>`."""
    msg = b"Link: https://lore.kernel.org/175852292275.307379@devnote2\n"
    assert extract_message_ids(msg) == ["175852292275.307379@devnote2"]


def test_extract_with_list_slug_and_trailing_slash():
    """`Link: https://lore.kernel.org/all/<msgid>/` (list-named
    slug, trailing slash), common when the committer pasted the
    URL from a browser."""
    msg = b"Link: https://lore.kernel.org/all/175824455687.45175.3734166065458520748.stgit@devnote2/\n"
    assert extract_message_ids(msg) == [
        "175824455687.45175.3734166065458520748.stgit@devnote2",
    ]


def test_extract_multiple_links():
    """A commit referencing multiple lore threads, rare but
    legal. Each Link: trailer adds a row."""
    msg = (
        b"Subject.\n\n"
        b"Link: https://lore.kernel.org/r/a@b\n"
        b"Link: https://lore.kernel.org/r/c@d\n"
        b"Signed-off-by: X <x@y>\n"
    )
    assert extract_message_ids(msg) == ["a@b", "c@d"]


def test_extract_ignores_non_lore_links():
    """Many commits carry `Link:` trailers to GitHub, Reddit, etc.
    Those reference issues, not lore msgids, must be ignored so
    we don't pollute the table with non-message-id strings."""
    msg = (
        b"Link: https://github.com/koverstreet/bcachefs/issues/1045\n"
        b"Link: https://www.reddit.com/r/bcachefs/comments/abc/\n"
        b"Link: https://lore.kernel.org/r/keep@me\n"
    )
    assert extract_message_ids(msg) == ["keep@me"]


def test_extract_returns_empty_when_no_link_trailer():
    msg = b"Just a subject and a Signed-off-by.\n\nSigned-off-by: X <x@y>\n"
    assert extract_message_ids(msg) == []


def test_extract_dedupes_same_msgid_across_link_variants():
    """Real commits sometimes carry the same Message-ID under both
    the `/r/` and `/all/` slug, or duplicate a Link trailer in a
    cherry-pick. Without dedup these collide on the
    `(commit_sha, message_id)` UNIQUE constraint and abort the
    batch (observed against linux.git commit 9e8e8912b05f, 1.15.0
    deploy)."""
    msg = (
        b"Subject.\n\n"
        b"Link: https://lore.kernel.org/r/same@x\n"
        b"Link: https://lore.kernel.org/all/same@x/\n"
        b"Signed-off-by: X <x@y>\n"
    )
    assert extract_message_ids(msg) == ["same@x"]


def test_extract_dedupe_preserves_order_of_distinct_msgids():
    """Dedup keeps the first occurrence of each msgid and preserves
    insertion order, order is what we'd want if a renderer ever
    sorts by appearance."""
    msg = (
        b"Link: https://lore.kernel.org/r/a@x\n"
        b"Link: https://lore.kernel.org/r/b@x\n"
        b"Link: https://lore.kernel.org/all/a@x/\n"
        b"Link: https://lore.kernel.org/r/c@x\n"
    )
    assert extract_message_ids(msg) == ["a@x", "b@x", "c@x"]


def test_extract_handles_non_decodable_bytes_via_surrogateescape():
    """A stray non-UTF-8 byte must not crash the extractor, those
    appear occasionally in older commits with contributor names
    in legacy encodings."""
    # 0xff is invalid UTF-8 but the regex doesn't care about that
    # region of the message; the Link line still matches.
    msg = (
        b"Co-developed-by: Jos\xff Doe <j@x>\nLink: https://lore.kernel.org/r/keep@me\n"
    )
    assert extract_message_ids(msg) == ["keep@me"]


def test_extract_quoted_link_in_body_still_matches():
    """The MULTILINE anchor is `^Link:`, quoted variants like
    `> Link:` don't match (good; that's a re-quote, not the
    commit's own trailer)."""
    msg = b"Subject.\n\n> Link: https://lore.kernel.org/r/not-mine@x\nLink: https://lore.kernel.org/r/mine@x\n"
    assert extract_message_ids(msg) == ["mine@x"]


# walk_commits, integration with dulwich + DB.


def _build_commit(
    repo: Repo,
    message: bytes,
    parent: bytes | None = None,
    commit_time: int = 1700000000,
) -> bytes:
    """Append a commit with `message` (empty tree) to the bare repo
    and update HEAD. Returns the new commit id."""
    tree = Tree()
    # Empty trees serialise to a fixed SHA; dulwich expects it to
    # be in the object store. Adding it is idempotent.
    repo.object_store.add_object(tree)
    commit = Commit()
    commit.tree = tree.id
    commit.parents = [parent] if parent else []
    commit.author = commit.committer = b"test <t@x>"
    commit.commit_time = commit.author_time = commit_time
    commit.commit_timezone = commit.author_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = message
    repo.object_store.add_object(commit)
    repo.refs[b"HEAD"] = commit.id
    return commit.id


def _bare_repo(path: Path) -> Repo:
    return Repo.init_bare(str(path), mkdir=True)


def test_walk_commits_inserts_rows_for_link_trailers(
    seeded_db,
    tmp_path,
    writer_thread,
):
    repo = _bare_repo(tmp_path / "tree.git")
    c1 = _build_commit(
        repo, b"Fix something.\n\nLink: https://lore.kernel.org/r/m1@x\n"
    )
    _build_commit(
        repo,
        b"Fix something else.\n\nLink: https://lore.kernel.org/r/m2@x\nLink: https://lore.kernel.org/r/m3@x\n",
        parent=c1,
    )
    with seeded_db() as s:
        result = walk_commits(s, tmp_path / "tree.git", writer=writer_thread)

    assert result.commits_seen == 2
    assert result.linked == 2
    assert result.rows_inserted == 3

    with seeded_db() as s:
        rows = {
            (r.message_id, r.tree_name)
            for r in s.execute(select(MainlineCommit)).scalars()
        }
    assert rows == {("m1@x", "linus"), ("m2@x", "linus"), ("m3@x", "linus")}


def test_walk_commits_resumes_from_cursor(seeded_db, tmp_path, writer_thread):
    """Second walk only sees the commits added since the prior
    cursor; that's the steady-state cheap path."""
    repo_path = tmp_path / "tree.git"
    repo = _bare_repo(repo_path)
    c1 = _build_commit(repo, b"first\n\nLink: https://lore.kernel.org/r/m1@x\n")

    with seeded_db() as s:
        result_1 = walk_commits(s, repo_path, writer=writer_thread)
    assert result_1.commits_seen == 1
    assert result_1.rows_inserted == 1

    # Append a second commit then walk again.
    _build_commit(repo, b"second\n\nLink: https://lore.kernel.org/r/m2@x\n", parent=c1)
    with seeded_db() as s:
        result_2 = walk_commits(s, repo_path, writer=writer_thread)
    assert result_2.commits_seen == 1  # only the new one
    assert result_2.rows_inserted == 1

    with seeded_db() as s:
        rows = {r.message_id for r in s.execute(select(MainlineCommit)).scalars()}
    assert rows == {"m1@x", "m2@x"}


def test_walk_commits_noop_when_head_unchanged(seeded_db, tmp_path, writer_thread):
    """Walking the same head twice in a row examines zero commits."""
    repo = _bare_repo(tmp_path / "tree.git")
    _build_commit(repo, b"only\n\nLink: https://lore.kernel.org/r/m@x\n")
    with seeded_db() as s:
        walk_commits(s, tmp_path / "tree.git", writer=writer_thread)
    with seeded_db() as s:
        result = walk_commits(s, tmp_path / "tree.git", writer=writer_thread)
    assert result.commits_seen == 0
    assert result.rows_inserted == 0


def test_walk_commits_skips_commits_without_link(seeded_db, tmp_path, writer_thread):
    """Most kernel commits don't carry a lore `Link:`, those
    consume a `commits_seen` slot but produce no rows. Pins the
    counter shape so the operator can see "walked N, linked K"
    progress."""
    repo = _bare_repo(tmp_path / "tree.git")
    c1 = _build_commit(repo, b"no lore link here\n\nSigned-off-by: X <x@y>\n")
    _build_commit(
        repo, b"another\n\nLink: https://github.com/x/y/issues/1\n", parent=c1
    )

    with seeded_db() as s:
        result = walk_commits(s, tmp_path / "tree.git", writer=writer_thread)
    assert result.commits_seen == 2
    assert result.linked == 0
    assert result.rows_inserted == 0


def test_walk_commits_records_commit_time(seeded_db, tmp_path, writer_thread):
    """`committed_at` carries the commit's timestamp; that's what
    the patch-page surface renders as "on <date>"."""
    repo = _bare_repo(tmp_path / "tree.git")
    # 2024-06-01 00:00:00 UTC
    ts = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp())
    _build_commit(
        repo,
        b"x\n\nLink: https://lore.kernel.org/r/m@x\n",
        commit_time=ts,
    )
    with seeded_db() as s:
        walk_commits(s, tmp_path / "tree.git", writer=writer_thread)
    with seeded_db() as s:
        row = s.execute(select(MainlineCommit)).scalar_one()
    # SQLite stores datetimes without tzinfo on round-trip; the
    # value is naive UTC by convention (same shape as
    # `Article.date`, see CONTEXT.md "tz-aware UTC normalization").
    # Render code attaches UTC at the consumer end.
    assert row.committed_at.replace(tzinfo=timezone.utc) == datetime(
        2024,
        6,
        1,
        tzinfo=timezone.utc,
    )


def test_walk_commits_advances_state_cursor(seeded_db, tmp_path, writer_thread):
    """After a walk, `MainlineState.commits_walked_to_sha` points
    at the most recent commit on HEAD. Otherwise the next walk
    would re-do work."""
    repo = _bare_repo(tmp_path / "tree.git")
    _build_commit(repo, b"x\n\nLink: https://lore.kernel.org/r/m@x\n")
    head_sha = repo.head().decode("ascii")
    with seeded_db() as s:
        walk_commits(s, tmp_path / "tree.git", writer=writer_thread)
    with seeded_db() as s:
        state = s.get(MainlineState, "linus")
    assert state is not None
    assert state.commits_walked_to_sha == head_sha


def test_walk_commits_rewalks_when_cursor_missing(seeded_db, tmp_path, writer_thread):
    """If the cursor points at a SHA no longer in the repo (history
    rewrite, shallow re-clone), the walker re-walks from scratch
    rather than crashing. Defensive, Linus's tree shouldn't
    history-rewrite, but stable trees occasionally do."""
    repo = _bare_repo(tmp_path / "tree.git")
    _build_commit(repo, b"x\n\nLink: https://lore.kernel.org/r/m@x\n")

    # Seed the state with a SHA that doesn't exist.
    with seeded_db() as s:
        s.add(
            MainlineState(
                tree_name="linus",
                commits_walked_to_sha="0" * 40,
            )
        )
        s.commit()
    with seeded_db() as s:
        result = walk_commits(s, tmp_path / "tree.git", writer=writer_thread)
    # Walker re-walks from the beginning despite the stale cursor.
    assert result.commits_seen == 1
    assert result.rows_inserted == 1


def _make_fake_tree(tmp_path, commits):
    """Build a minimal bare git repo at tmp_path/fake.git with the
    given linear commits. `commits` is a list of (message, filename)
    tuples; each becomes a commit whose tree has just that file."""
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo

    repo_path = tmp_path / "fake.git"
    repo = Repo.init_bare(str(repo_path), mkdir=True)
    parent = None
    for msg, filename in commits:
        blob = Blob.from_string(b"x")
        repo.object_store.add_object(blob)
        tree = Tree()
        tree.add(filename, 0o100644, blob.id)
        repo.object_store.add_object(tree)
        commit = Commit()
        commit.tree = tree.id
        commit.message = msg.encode()
        commit.author = commit.committer = b"Test <t@x>"
        commit.author_time = commit.commit_time = 1700000000
        commit.author_timezone = commit.commit_timezone = 0
        if parent:
            commit.parents = [parent]
        repo.object_store.add_object(commit)
        parent = commit.id
    if parent is not None:
        repo.refs[b"HEAD"] = parent
    return repo_path


def _make_fake_tree_with_branches(tmp_path, branches):
    """Like _make_fake_tree but builds N independent branches. `branches`
    is a dict {branch_name: [(message, filename), ...]} where the special
    name "HEAD" goes on HEAD and any other name becomes a
    `refs/heads/<name>` branch."""
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo

    repo_path = tmp_path / "fake.git"
    repo = Repo.init_bare(str(repo_path), mkdir=True)
    for branch_name, commits in branches.items():
        parent = None
        for msg, filename in commits:
            blob = Blob.from_string(b"x")
            repo.object_store.add_object(blob)
            tree = Tree()
            tree.add(filename, 0o100644, blob.id)
            repo.object_store.add_object(tree)
            commit = Commit()
            commit.tree = tree.id
            commit.message = msg.encode()
            commit.author = commit.committer = b"Test <t@x>"
            commit.author_time = commit.commit_time = 1700000000
            commit.author_timezone = commit.commit_timezone = 0
            if parent:
                commit.parents = [parent]
            repo.object_store.add_object(commit)
            parent = commit.id
        if branch_name == "HEAD":
            repo.refs[b"HEAD"] = parent
        else:
            repo.refs[f"refs/heads/{branch_name}".encode()] = parent
    return repo_path


def test_walk_commits_rebases_true_clears_old_rows_for_tree(
    seeded_db, tmp_path, writer_thread
):
    """rebases=True walker DELETEs existing rows for this tree before
    walking. Stale SHAs from a prior force-pushed history don't
    accumulate."""
    from mimir.models import MainlineCommit

    with seeded_db() as s:
        s.add(
            MainlineCommit(
                commit_sha="staleshastaleshastaleshastalesha000000000",
                message_id="orphan@example.com",
                tree_name="linux-next",
                committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        s.commit()

    tree_path = _make_fake_tree(
        tmp_path,
        commits=[
            (
                "Some change\n\nLink: https://lore.kernel.org/r/live@example.com\n",
                b"foo.c",
            ),
        ],
    )
    with seeded_db() as s:
        walk_commits(
            s, tree_path, tree_name="linux-next", rebases=True, writer=writer_thread
        )

    with seeded_db() as s:
        surviving = {
            r.commit_sha
            for r in s.execute(
                select(MainlineCommit).where(MainlineCommit.tree_name == "linux-next")
            ).scalars()
        }
        assert "staleshastaleshastaleshastalesha000000000" not in surviving
        live_mids = {r.message_id for r in s.execute(select(MainlineCommit)).scalars()}
        assert "live@example.com" in live_mids


def test_walk_commits_rebases_false_keeps_other_tree_rows(
    seeded_db, tmp_path, writer_thread
):
    """rebases=False walking net-next does NOT touch linus rows."""
    from mimir.models import MainlineCommit

    other_sha = "linushashasalinushashasalinushashasalinush"
    with seeded_db() as s:
        s.add(
            MainlineCommit(
                commit_sha=other_sha,
                message_id="other@example.com",
                tree_name="linus",
                committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        s.commit()

    # Real commit list so the walker actually exercises its main loop;
    # otherwise an empty repo short-circuits before any tree-isolation
    # logic runs and the test proves nothing.
    tree_path = _make_fake_tree(
        tmp_path,
        commits=[
            ("net-next change\n\nLink: https://lore.kernel.org/r/netfix@x\n", b"f.c"),
        ],
    )
    with seeded_db() as s:
        walk_commits(
            s, tree_path, tree_name="net-next", rebases=False, writer=writer_thread
        )

    with seeded_db() as s:
        survived = s.get(MainlineCommit, (other_sha, "other@example.com"))
        assert survived is not None


def test_walk_commits_branch_override(seeded_db, tmp_path, writer_thread):
    """Non-default branch param walks that branch, not HEAD."""
    from mimir.models import MainlineCommit

    tree_path = _make_fake_tree_with_branches(
        tmp_path,
        branches={
            "HEAD": [
                (
                    "HEAD-commit\n\nLink: https://lore.kernel.org/r/head@x\n",
                    b"f.c",
                )
            ],
            "mm-stable": [
                (
                    "mm-stable-commit\n\nLink: https://lore.kernel.org/r/stable@x\n",
                    b"f.c",
                )
            ],
        },
    )
    with seeded_db() as s:
        walk_commits(
            s, tree_path, tree_name="mm", branch="mm-stable", writer=writer_thread
        )

    with seeded_db() as s:
        mids = {
            r.message_id
            for r in s.execute(
                select(MainlineCommit).where(MainlineCommit.tree_name == "mm")
            ).scalars()
        }
    assert "stable@x" in mids
    assert "head@x" not in mids


def test_walk_commits_exclude_from_skips_commits_reachable_from_ref(
    seeded_db, tmp_path, writer_thread
):
    """exclude_from skips commits reachable from another tree's head.
    Pragmatic optimisation: non-Linus trees cloned with
    --reference linus.git can pass linus_head here and avoid walking
    the shared history. INSERT-OR-IGNORE protects correctness if it
    were ever applied incorrectly; this test pins the perf-side
    behaviour (we actually skip the work, not just dedup it)."""
    from dulwich.repo import Repo

    from mimir.models import MainlineCommit

    tree_path = _make_fake_tree(
        tmp_path,
        commits=[
            ("c1\n\nLink: https://lore.kernel.org/r/c1@x\n", b"a.c"),
            ("c2\n\nLink: https://lore.kernel.org/r/c2@x\n", b"a.c"),
            ("c3\n\nLink: https://lore.kernel.org/r/c3@x\n", b"a.c"),
            ("c4\n\nLink: https://lore.kernel.org/r/c4@x\n", b"a.c"),
        ],
    )
    # Get the SHA of the second commit (c2); use it as exclude_from.
    repo = Repo(str(tree_path))
    walker = repo.get_walker(include=[repo.head()], reverse=True)
    shas = [e.commit.id for e in walker]
    c2_sha = shas[1]  # c1, c2, c3, c4 oldest-first

    with seeded_db() as s:
        walk_commits(
            s,
            tree_path,
            tree_name="downstream",
            exclude_from=c2_sha,
            writer=writer_thread,
        )

    with seeded_db() as s:
        mids = {
            r.message_id
            for r in s.execute(
                select(MainlineCommit).where(MainlineCommit.tree_name == "downstream")
            ).scalars()
        }
    # Only commits AFTER c2 emitted: c3 and c4. c1 and c2 excluded.
    assert mids == {"c3@x", "c4@x"}


def test_walk_commits_rebases_true_ignores_exclude_from(
    seeded_db, tmp_path, writer_thread
):
    """rebases=True trees keep their full DELETE-and-rewalk semantics
    even when exclude_from is supplied. Applying the shortcut on a
    daily linux-next rebase would erase intermediate-tree
    mainline_commits rows for patches that transition into Linus that
    day, losing the per-tree timeline event on the patch page."""
    from dulwich.repo import Repo

    from mimir.models import MainlineCommit

    tree_path = _make_fake_tree(
        tmp_path,
        commits=[
            ("c1\n\nLink: https://lore.kernel.org/r/c1@x\n", b"a.c"),
            ("c2\n\nLink: https://lore.kernel.org/r/c2@x\n", b"a.c"),
        ],
    )
    head = Repo(str(tree_path)).head()

    with seeded_db() as s:
        walk_commits(
            s,
            tree_path,
            tree_name="rebase-tree",
            rebases=True,
            exclude_from=head,
            writer=writer_thread,
        )

    with seeded_db() as s:
        mids = {
            r.message_id
            for r in s.execute(
                select(MainlineCommit).where(MainlineCommit.tree_name == "rebase-tree")
            ).scalars()
        }
    # All commits walked: exclude_from is ignored when rebases=True.
    assert mids == {"c1@x", "c2@x"}


def test_ensure_tree_passes_reference_to_clone(tmp_path, monkeypatch):
    """When reference is set, git clone is invoked with --reference."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        # Simulate a successful clone by creating the dir.
        from pathlib import Path as _P

        # Args after `--` are: url, path
        dd_idx = cmd.index("--")
        path = _P(cmd[dd_idx + 2])
        path.mkdir(parents=True, exist_ok=True)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr("mimir.mainline.subprocess.run", fake_run)
    from mimir.config import TreeConfig
    from mimir.mainline import _ensure_tree

    tree = TreeConfig(
        url="https://example.com/foo.git",
        path=tmp_path / "foo.git",
    )
    linus_path = tmp_path / "linus.git"
    linus_path.mkdir()
    _ensure_tree(tree, reference=linus_path, skip_fetch=False)
    clone_cmd = calls[0]
    assert "--reference" in clone_cmd
    ref_idx = clone_cmd.index("--reference")
    assert clone_cmd[ref_idx + 1] == str(linus_path)


def test_ensure_tree_no_reference_for_linus(tmp_path, monkeypatch):
    """reference=None -> no --reference flag in the git clone command."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        from pathlib import Path as _P

        dd_idx = cmd.index("--")
        path = _P(cmd[dd_idx + 2])
        path.mkdir(parents=True, exist_ok=True)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr("mimir.mainline.subprocess.run", fake_run)
    from mimir.config import TreeConfig
    from mimir.mainline import _ensure_tree

    tree = TreeConfig(
        url="https://example.com/linus.git",
        path=tmp_path / "linus.git",
    )
    _ensure_tree(tree, reference=None, skip_fetch=False)
    assert "--reference" not in calls[0]


def test_walk_commits_full_rewalk_is_idempotent_via_on_conflict(
    seeded_db,
    tmp_path,
    writer_thread,
):
    """A full rewalk over an already-populated table must not
    crash on the `(commit_sha, message_id)` UNIQUE constraint
    the INSERT runs with `ON CONFLICT DO NOTHING`. Hit on the
    1.15.0 / 1.15.1 production runs against linux.git, where
    dulwich's reverse-walker re-emitted some commits across batch
    boundaries (or via merge-graph paths) and the second
    observation collided with the first. The 1.15.3 fix is to
    swallow conflicts at the DB layer."""
    repo_path = tmp_path / "tree.git"
    repo = _bare_repo(repo_path)
    _build_commit(repo, b"a\n\nLink: https://lore.kernel.org/r/m@x\n")

    # First walk: populates the row.
    with seeded_db() as s:
        result_1 = walk_commits(s, repo_path, writer=writer_thread)
    assert result_1.commits_seen == 1

    # Force-clear the cursor so the next walk re-walks from
    # scratch. Without the ON CONFLICT clause this raised
    # IntegrityError on the existing row.
    with seeded_db() as s:
        state = s.get(MainlineState, "linus")
        state.commits_walked_to_sha = None
        s.commit()

    with seeded_db() as s:
        result_2 = walk_commits(s, repo_path, writer=writer_thread)
    assert result_2.commits_seen == 1
    # rows_inserted counts observations the walker emitted; the
    # DB silently dropped the duplicate, so the table row count
    # stays at 1 (not 2).
    with seeded_db() as s:
        row_count = s.scalar(select(func.count(MainlineCommit.commit_sha)))
    assert row_count == 1


def test_update_mainline_skips_tree_not_yet_due(seeded_db, monkeypatch, tmp_path):
    """A tree whose last_walked_at is recent enough is skipped."""
    from datetime import datetime, timezone

    from mimir.config import TreeConfig, settings
    from mimir.mainline import update_mainline
    from mimir.models import MainlineState

    # Override settings.trees with one fake tree for the test
    monkeypatch.setattr(
        settings,
        "trees",
        {
            "fake": TreeConfig(
                url="https://example.com/fake.git",
                path=tmp_path / "fake.git",
                walk_every_seconds=3600,
                rebases=False,
            ),
        },
    )
    # last_walked_at = now -> skipped on next call
    with seeded_db() as s:
        state = MainlineState(
            tree_name="fake",
            last_walked_at=datetime.now(timezone.utc),
        )
        s.add(state)
        s.commit()

    # _ensure_tree must NOT be called (tree skipped).
    called = {"ensure": False}

    def fake_ensure(*a, **kw):
        called["ensure"] = True
        return tmp_path

    monkeypatch.setattr("mimir.mainline._ensure_tree", fake_ensure)

    result = update_mainline(skip_maintainers=True, skip_commits=False)
    assert called["ensure"] is False, (
        "ensure_tree should not be called for not-due tree"
    )
    assert "fake" in result.trees
    assert result.trees["fake"].skipped is True


def test_update_mainline_per_tree_failure_isolated(seeded_db, monkeypatch, tmp_path):
    """One tree raising doesn't fail the others."""
    from mimir.broker import _context
    from mimir.broker.pools import ReadSessionPool
    from mimir.broker.writes import WriterThread
    from mimir.config import TreeConfig, settings
    from mimir.mainline import WalkResult, update_mainline

    monkeypatch.setattr(
        settings,
        "trees",
        {
            "bad": TreeConfig(
                url="https://example.com/bad.git",
                path=tmp_path / "bad.git",
            ),
            "good": TreeConfig(
                url="https://example.com/good.git",
                path=tmp_path / "good.git",
            ),
        },
    )

    def fake_ensure(tree, *, reference, skip_fetch):
        if tree.url.endswith("bad.git"):
            raise RuntimeError("clone failed")
        from pathlib import Path as _P

        _P(tree.path).mkdir(parents=True, exist_ok=True)
        return tree.path

    monkeypatch.setattr("mimir.mainline._ensure_tree", fake_ensure)
    monkeypatch.setattr("mimir.mainline.walk_commits", lambda *a, **kw: WalkResult())

    # Phase 3: update_mainline fetches the active pool + writer from
    # _context when skip_commits=False. Register them for this test.
    pool = ReadSessionPool.from_settings()
    writer = WriterThread.from_settings()
    writer.start()
    try:
        _context.set_active(pool, writer)
        result = update_mainline(skip_maintainers=True)
    finally:
        _context.clear_active()
        writer.stop(timeout=10)
        pool.close()

    assert result.trees["bad"].ok is False
    assert result.trees["bad"].error is not None
    assert result.trees["good"].ok is True


def test_walk_commits_closes_repo_at_function_exit(
    seeded_db, tmp_path, writer_thread, monkeypatch
):
    """Pin the with-Repo lifecycle: walk_commits must close its Repo
    deterministically at function exit, not rely on GC. Spying on
    __exit__ because the bare-assignment path never invokes it
    (dulwich's Repo.__del__ does its own cleanup but does NOT go
    through __exit__). The spy unambiguously differentiates
    pre-fix from post-fix."""
    from dulwich.repo import Repo as DulwichRepo
    from mimir.extensions import SessionLocal

    exit_calls: list[bool] = []
    original_exit = DulwichRepo.__exit__

    def tracking_exit(self, *args):
        exit_calls.append(True)
        return original_exit(self, *args)

    monkeypatch.setattr(DulwichRepo, "__exit__", tracking_exit)

    _bare_repo(tmp_path / "tree.git")
    with SessionLocal() as s:
        walk_commits(s, tmp_path / "tree.git", writer=writer_thread)

    assert exit_calls, "Repo.__exit__ was not called during walk_commits"
