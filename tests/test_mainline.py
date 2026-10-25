"""Tests for `mimir.mainline`, Link-trailer extraction +
the dulwich-driven commit walker.

Real-world Link trailers (sampled from the upstream Linus tree) are
included as parser fixtures; the walker tests use fake bare repos
built with dulwich so the suite stays offline + fast.
"""

import functools
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dulwich.objects import Commit, Tree
from dulwich.repo import Repo
from sqlalchemy import create_engine, func, select, text

from mimir.broker.writes import WriteOp, WriterThread
from mimir.config import settings
from mimir.mainline import _submit_mainline_batch, extract_message_ids, walk_commits
from mimir.models import Inbox, MainlineCommit, MainlineState
from tests._helpers import add_commit


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
    `(commit_sha, message_id, tree_name)` primary key and abort the
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


def test_extract_patch_msgid_link():
    """b4's default trailer since 2024 (#668): most b4-applied commits
    carry only this form."""
    msg = b"Fix.\n\nLink: https://patch.msgid.link/20251001-x-v1-1-abc@kernel.org\n"
    assert extract_message_ids(msg) == ["20251001-x-v1-1-abc@kernel.org"]


def test_extract_dedupes_patch_msgid_link_against_lore_form():
    msg = (
        b"Link: https://lore.kernel.org/r/same@x\n"
        b"Link: https://patch.msgid.link/same@x\n"
    )
    assert extract_message_ids(msg) == ["same@x"]


def test_extract_message_id_trailer():
    """`git am --message-id` writes the posted patch's Message-ID as a
    trailer; same claim as a `Link:`, so same row (#668)."""
    msg = b"Fix.\n\nMessage-ID: <abc@x>\nSigned-off-by: X <x@y>\n"
    assert extract_message_ids(msg) == ["abc@x"]


def test_extract_message_id_trailer_dedupes_and_keeps_order():
    msg = b"Link: https://lore.kernel.org/r/a@x\nMessage-Id: <b@x>\nMessage-ID: <a@x>\n"
    assert extract_message_ids(msg) == ["a@x", "b@x"]


def test_extract_message_id_only_in_trailer_block():
    """A body that quotes a bug report's headers is not a claim about
    the patch: only the final paragraph is the trailer block."""
    msg = (
        b"Fix the crash from this report:\n\n"
        b"From: Reporter <r@x>\n"
        b"Message-ID: <report-123@example.com>\n\n"
        b"Signed-off-by: A <a@x>\n"
        b"Message-ID: <patch-1@kernel.org>\n"
    )
    assert extract_message_ids(msg) == ["patch-1@kernel.org"]


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
    commit = add_commit(
        repo,
        b"test <t@x>",
        [parent] if parent else [],
        message,
        when=commit_time,
        encoding=b"UTF-8",
    )
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
    ts = int(datetime(2024, 6, 1, tzinfo=UTC).timestamp())
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
    assert row.committed_at.replace(tzinfo=UTC) == datetime(
        2024,
        6,
        1,
        tzinfo=UTC,
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


def test_walk_commits_rewalk_reads_history_behind_the_cursor(
    seeded_db, tmp_path, writer_thread
):
    """A trailer-pattern fix only reaches commits walked after it, so
    `rewalk` ignores the cursor and reads the whole history again
    (#668). Rows already present are absorbed by ON CONFLICT."""
    repo_path = tmp_path / "tree.git"
    repo = _bare_repo(repo_path)
    c1 = _build_commit(repo, b"old\n\nLink: https://lore.kernel.org/r/m1@x\n")
    _build_commit(repo, b"newer\n\nLink: https://lore.kernel.org/r/m2@x\n", parent=c1)
    with seeded_db() as s:
        walk_commits(s, repo_path, writer=writer_thread)

    with seeded_db() as s:
        plain = walk_commits(s, repo_path, writer=writer_thread)
    assert plain.commits_seen == 0  # precondition: the cursor is at HEAD

    with seeded_db() as s:
        again = walk_commits(s, repo_path, writer=writer_thread, rewalk=True)
    assert again.commits_seen == 2
    with seeded_db() as s:
        assert s.get(MainlineState, "linus").commits_walked_to_sha == (
            repo.head().decode("ascii")
        )


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
    """rebases=True walker drops rows the tree no longer holds. Stale
    SHAs from a prior force-pushed history don't accumulate."""
    from mimir.models import MainlineCommit

    with seeded_db() as s:
        s.add(
            MainlineCommit(
                commit_sha="staleshastaleshastaleshastalesha000000000",
                message_id="orphan@example.com",
                tree_name="linux-next",
                committed_at=datetime(2026, 1, 1, tzinfo=UTC),
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
                committed_at=datetime(2026, 1, 1, tzinfo=UTC),
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
        survived = s.get(MainlineCommit, (other_sha, "other@example.com", "linus"))
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


def test_linus_records_a_commit_a_subsystem_tree_recorded_first(
    seeded_db, tmp_path, writer_thread
):
    """#673. A patch is normally applied in a subsystem tree before
    Linus merges it, with the same SHA. When the key was
    `(commit_sha, message_id)`, the subsystem tree's row took the key
    and the Linus insert was ignored, so the patch never showed as
    LANDED. Each tree keeps its own row now."""
    from mimir.lifecycle_status import (
        LifecycleStatus,
        lifecycle_status_for_articles,
    )
    from tests.test_lifecycle_status import _seed_article

    tree_path = _make_fake_tree(
        tmp_path,
        commits=[("c1\n\nLink: https://lore.kernel.org/r/c1@x\n", b"a.c")],
    )
    with seeded_db() as s:
        art_id = _seed_article(s, "c1@x").id
    for tree_name in ("net-next", "linus"):
        with seeded_db() as s:
            walk_commits(s, tree_path, tree_name=tree_name, writer=writer_thread)

    with seeded_db() as s:
        trees = s.scalars(
            select(MainlineCommit.tree_name).where(MainlineCommit.message_id == "c1@x")
        ).all()
        assert sorted(trees) == ["linus", "net-next"]
        got = lifecycle_status_for_articles(
            s, [art_id], s.scalar(select(Inbox.id).where(Inbox.name == "lkml"))
        )
    assert got[art_id].state == LifecycleStatus.LANDED


def test_walk_commits_rebases_true_applies_exclude_from(
    seeded_db, tmp_path, writer_thread
):
    """rebases=True trees skip commits Linus already has, like every
    other non-Linus tree. Without this, linux-next walked its whole
    history daily and, once each tree kept its own rows (#673), stored
    a row for nearly every linked commit in Linus's tree."""
    from dulwich.repo import Repo

    from mimir.models import MainlineCommit

    tree_path = _make_fake_tree(
        tmp_path,
        commits=[
            ("c1\n\nLink: https://lore.kernel.org/r/c1@x\n", b"a.c"),
            ("c2\n\nLink: https://lore.kernel.org/r/c2@x\n", b"a.c"),
        ],
    )
    repo = Repo(str(tree_path))
    c1_sha = next(iter(repo.get_walker(include=[repo.head()], reverse=True))).commit.id

    with seeded_db() as s:
        walk_commits(
            s,
            tree_path,
            tree_name="rebase-tree",
            rebases=True,
            exclude_from=c1_sha,
            writer=writer_thread,
        )

    with seeded_db() as s:
        mids = {
            r.message_id
            for r in s.execute(
                select(MainlineCommit).where(MainlineCommit.tree_name == "rebase-tree")
            ).scalars()
        }
    assert mids == {"c2@x"}


@pytest.mark.allow_tree_clone
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


@pytest.mark.allow_tree_clone
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
    crash on the `(commit_sha, message_id, tree_name)` primary key
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
    from datetime import datetime

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
            last_walked_at=datetime.now(UTC),
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


def test_load_maintainers_closes_repo_at_function_exit(
    seeded_db, tmp_path, monkeypatch
):
    """Same lifecycle pin as walk_commits but for load_maintainers,
    which opens a Repo, reads MAINTAINERS at HEAD, and writes the
    parsed subsystems triple. The Repo close was previously
    GC-deferred; now deterministic at function exit."""
    from dulwich.objects import Blob
    from dulwich.repo import Repo as DulwichRepo

    from mimir.mainline import load_maintainers

    exit_calls: list[bool] = []
    original_exit = DulwichRepo.__exit__

    def tracking_exit(self, *args):
        exit_calls.append(True)
        return original_exit(self, *args)

    monkeypatch.setattr(DulwichRepo, "__exit__", tracking_exit)

    # Build a bare repo with a HEAD commit whose tree contains a
    # minimal MAINTAINERS blob. _bare_repo seeds an empty repo; we
    # need at least one commit + MAINTAINERS for load_maintainers to
    # find the blob.
    repo = _bare_repo(tmp_path / "tree.git")
    blob = Blob.from_string(
        b"BCACHEFS\nM:\tKent Overstreet <kent@example.org>\nF:\tfs/bcachefs/\n"
    )
    tree = Tree()
    tree.add(b"MAINTAINERS", 0o100644, blob.id)
    commit = Commit()
    commit.tree = tree.id
    commit.author = commit.committer = b"Test <test@example.org>"
    commit.author_time = commit.commit_time = 1700000000
    commit.author_timezone = commit.commit_timezone = 0
    commit.message = b"seed MAINTAINERS"
    repo.object_store.add_object(blob)
    repo.object_store.add_object(tree)
    repo.object_store.add_object(commit)
    repo.refs[b"HEAD"] = commit.id
    repo.close()

    load_maintainers(tmp_path / "tree.git", tree_name="linus", force=True)

    assert exit_calls, "Repo.__exit__ was not called during load_maintainers"


def _commit_maintainers(repo, maintainers_bytes, *, parent=None, other=None, ts):
    """Add a commit whose tree holds `maintainers_bytes` as MAINTAINERS
    and, optionally, `other` bytes as an unrelated file. Returns the
    commit id."""
    from dulwich.objects import Blob

    tree = Tree()
    blob = Blob.from_string(maintainers_bytes)
    repo.object_store.add_object(blob)
    tree.add(b"MAINTAINERS", 0o100644, blob.id)
    if other is not None:
        other_blob = Blob.from_string(other)
        repo.object_store.add_object(other_blob)
        tree.add(b"Makefile", 0o100644, other_blob.id)
    repo.object_store.add_object(tree)
    commit = Commit()
    commit.tree = tree.id
    commit.parents = [parent] if parent else []
    commit.author = commit.committer = b"Test <test@example.org>"
    commit.author_time = commit.commit_time = ts
    commit.author_timezone = commit.commit_timezone = 0
    commit.message = b"c"
    repo.object_store.add_object(commit)
    repo.refs[b"HEAD"] = commit.id
    return commit.id


def test_maintainers_reload_is_gated_on_the_blob_not_on_head(seeded_db, tmp_path):
    """A push that does not touch MAINTAINERS must not reparse.

    Linus's tree is pushed to many times a day; MAINTAINERS changes far
    more rarely. Gating on HEAD meant re-reading the file and rewriting
    the whole ~15k-row subsystems triple to identical values on most
    ticks, on the single-writer broker. It also rotated
    `last_commit_sha`, which the web tier folds into every page's ETag
    as the subsystem-rule version, so the entire archive's validators
    moved on every push while the rendered output was unchanged.

    The cursor must therefore track the MAINTAINERS BLOB. Asserted in
    both directions: unchanged content does not reparse even though
    HEAD moved, and changed content does.
    """
    from mimir.extensions import SessionLocal
    from mimir.mainline import load_maintainers
    from mimir.models import MainlineState

    def cursor():
        with SessionLocal() as s:
            return s.scalar(
                select(MainlineState.last_commit_sha).where(
                    MainlineState.tree_name == "linus"
                )
            )

    original = b"BCACHEFS\nM:\tKent Overstreet <kent@example.org>\nF:\tfs/bcachefs/\n"
    path = tmp_path / "tree.git"
    repo = _bare_repo(path)
    c1 = _commit_maintainers(repo, original, ts=1700000000)
    repo.close()

    ran, _loaded, head1 = load_maintainers(path, tree_name="linus")
    assert ran, "first load must parse"
    after_first = cursor()

    # A push that leaves MAINTAINERS byte-identical. HEAD moves.
    repo = Repo(str(path))
    c2 = _commit_maintainers(
        repo, original, parent=c1, other=b"# unrelated", ts=1700000100
    )
    repo.close()
    assert c2 != c1

    ran, _loaded, head2 = load_maintainers(path, tree_name="linus")
    assert head2 != head1, "HEAD did move, so this is the case that matters"
    assert not ran, "reparsed despite MAINTAINERS being byte-identical"
    assert cursor() == after_first, (
        "cursor moved on an unrelated push, which rotates every page ETag"
    )

    # Now actually edit MAINTAINERS.
    repo = Repo(str(path))
    _commit_maintainers(
        repo, original + b"\nSOMETHING\nF:\tdrivers/x/\n", parent=c2, ts=1700000200
    )
    repo.close()

    ran, _loaded, _head3 = load_maintainers(path, tree_name="linus")
    assert ran, "a real MAINTAINERS edit must reparse"
    assert cursor() != after_first, "cursor must move when the content changed"


def test_forced_reparse_moves_the_rules_cursor_even_when_the_blob_is_unchanged(
    seeded_db, tmp_path
):
    """`--force` must not rebuild the rules while freezing the ETag.

    The web tier reads this cursor as the subsystem-rule version. A
    forced reparse is the operator asserting the rules may differ from
    what the bytes imply (a parser fix, a hand-repaired triple), and it
    does rebuild the triple and drop the rules snapshot, so the origin
    starts rendering the new subsystem lines. If the cursor were
    re-stored unchanged, the CDN and every crawler would hold a
    validator that still matches and keep serving the contradicted body
    off a 304, with no expiry able to break it.

    Gating on the content half means this costs no extra reparse: the
    next natural tick still no-ops.
    """
    from mimir.extensions import SessionLocal
    from mimir.mainline import load_maintainers
    from mimir.models import MainlineState

    def cursor():
        with SessionLocal() as s:
            return s.scalar(
                select(MainlineState.last_commit_sha).where(
                    MainlineState.tree_name == "linus"
                )
            )

    path = tmp_path / "tree.git"
    repo = _bare_repo(path)
    _commit_maintainers(repo, b"A\nM:\tx <x@example.org>\nF:\tfs/a/\n", ts=1700000000)
    repo.close()

    load_maintainers(path, tree_name="linus")
    before = cursor()

    ran, _loaded, _head = load_maintainers(path, tree_name="linus", force=True)
    assert ran, "--force must reparse"
    forced = cursor()
    assert forced != before, (
        "forced rebuild left the rule version unchanged, which freezes every "
        "page ETag while the rendered rules change"
    )

    # Forcing again must move it AGAIN. `--force` is by construction
    # the thing an operator repeats (fix the parser, force, inspect,
    # force again), and a generation that is merely present rather than
    # advancing re-freezes the validator on the second run: the same
    # bug, one iteration later.
    ran, _loaded, _head = load_maintainers(path, tree_name="linus", force=True)
    assert ran
    twice = cursor()
    assert twice != forced, "a repeated --force re-froze the rule version"

    # ...and the gate still recognises the content, so the next
    # ordinary tick does not pay for another rebuild.
    ran, _loaded, _head = load_maintainers(path, tree_name="linus")
    assert not ran, "generation suffix leaked into the reparse gate"
    assert cursor() == twice, "an ordinary no-op tick must not move the cursor"


def test_read_linus_head_closes_repo_at_function_exit(tmp_path, monkeypatch):
    """The linus-head probe was inline in update_mainline. Extracted
    into a `_read_linus_head` helper for testability + deterministic
    close. Same __exit__-spy shape as the walk_commits and
    load_maintainers tests."""
    from dulwich.repo import Repo as DulwichRepo

    from mimir.mainline import _read_linus_head

    exit_calls: list[bool] = []
    original_exit = DulwichRepo.__exit__

    def tracking_exit(self, *args):
        exit_calls.append(True)
        return original_exit(self, *args)

    monkeypatch.setattr(DulwichRepo, "__exit__", tracking_exit)

    # _read_linus_head's contract is "return None on any failure"
    # but to exercise __exit__ we need a Repo whose HEAD is readable.
    # Build a one-commit fixture (re-using the load_maintainers test
    # pattern).
    repo = _bare_repo(tmp_path / "linus.git")
    tree = Tree()
    commit = Commit()
    commit.tree = tree.id
    commit.author = commit.committer = b"Test <test@example.org>"
    commit.author_time = commit.commit_time = 1700000000
    commit.author_timezone = commit.commit_timezone = 0
    commit.message = b"seed"
    repo.object_store.add_object(tree)
    repo.object_store.add_object(commit)
    repo.refs[b"HEAD"] = commit.id
    repo.close()

    head = _read_linus_head(tmp_path / "linus.git")

    assert head == commit.id, f"expected {commit.id!r}, got {head!r}"
    assert exit_calls, "Repo.__exit__ was not called during _read_linus_head"


def test_read_linus_head_returns_none_on_missing_repo(tmp_path):
    """Contract: _read_linus_head must NEVER raise; it returns None
    on any failure (missing clone, unreadable, no HEAD, etc.).
    Callers (update_mainline) fall back to walking full history."""
    from mimir.mainline import _read_linus_head

    assert _read_linus_head(tmp_path / "does-not-exist") is None


def test_a_missing_tree_path_raises_instead_of_cloning(tmp_path, monkeypatch):
    """The fresh-checkout guard, asserted rather than assumed.

    `_ensure_tree` clones when its target path is absent, and
    `skip_fetch` does not prevent that: it skips the fetch on an
    EXISTING clone only. So on any machine that already has the kernel
    tree every test passes, while a fresh checkout silently performs a
    ~3.6 GB `git clone` of torvalds/linux. Invisible to everyone who
    has the tree, which is why it survived long enough to become a
    standing "do not run the full suite" warning in subagent briefs.

    The autouse `_no_surprise_clone` fixture converts that into an
    immediate failure naming the tree and URL. This test pins the
    guard itself; without it the guard is one accidental edit from
    being a no-op that nobody notices until CI downloads a kernel.
    """
    import subprocess

    from mimir.config import TreeConfig
    from mimir.mainline import _ensure_tree

    def _explode(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError(f"a clone was attempted: {a!r}")

    monkeypatch.setattr(subprocess, "run", _explode)

    tree = TreeConfig(
        url="https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
        path=tmp_path / "definitely-absent",
    )
    # BaseException, not AssertionError: the guard deliberately raises
    # outside the `except Exception` that `update_mainline` wraps each
    # tree in. Pinning AssertionError here would re-admit the swallowed
    # form.
    with pytest.raises(BaseException, match="would `git clone`"):
        _ensure_tree(tree, reference=None, skip_fetch=True)


def test_the_clone_guard_survives_update_mainlines_per_tree_except(
    seeded_db, tmp_path, monkeypatch
):
    """The guard must reach the TEST, not just the application's logger.

    `update_mainline` wraps each tree's work in `except Exception:` and
    turns any failure into a WARNING plus `tr.ok = False` (deliberately:
    one broken tree must not abort the tick). `AssertionError` is an
    `Exception`, so the `_no_surprise_clone` guard is swallowed on the
    only production path that calls `_ensure_tree`. Nothing fails; the
    tick reports a failed tree and the test that drove it passes.

    That matters because it is exactly how a would-be clone reaches the
    suite. `test_update_mainline_uses_writer_thread_via_active_context` calls
    `update_mainline()` against the REAL `settings.trees` (seven trees,
    no `linus_tree` monkeypatch) and asserts only `result is not None`.
    On a machine that has `Mainline/*.git` it exercises the writer path
    for real; on a fresh checkout it used to clone seven kernel trees,
    and with the guard as written it now silently skips every tree and
    still passes, having reached neither `_ensure_tree`'s successor code
    nor the writer dispatch the test exists to pin. The suite's meaning
    becomes machine-dependent, which is the same blind spot the guard
    was written to close.

    A test-infrastructure guard therefore has to raise something the
    code under test cannot catch: `pytest.fail(..., pytrace=False)`
    (`Failed` derives from `BaseException`), or a private
    `BaseException` subclass. Production code is unchanged either way.
    """
    from mimir.config import TreeConfig, settings
    from mimir.mainline import update_mainline

    monkeypatch.setattr(
        settings,
        "trees",
        {
            "linus": TreeConfig(
                url=(
                    "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"
                ),
                path=tmp_path / "definitely-absent",
                walk_every_seconds=0,
            )
        },
    )

    # BaseException, not Exception: an `except Exception` handler in the
    # code under test must not be able to absorb a test guard.
    with pytest.raises(BaseException, match="would `git clone`"):
        update_mainline(
            skip_fetch=True,
            skip_maintainers=True,
            skip_commits=True,
        )


# Rebuild by difference (#671): a rebasing tree's rows change in one
# step, so a reader never sees a partial tree.


def _tree_keys(seeded_db, tree_name):
    with seeded_db() as s:
        return set(
            s.execute(
                select(MainlineCommit.commit_sha, MainlineCommit.message_id).where(
                    MainlineCommit.tree_name == tree_name
                )
            ).tuples()
        )


def _seed_rows(seeded_db, tree_name, keys):
    with seeded_db() as s:
        for sha, mid in keys:
            s.add(
                MainlineCommit(
                    commit_sha=sha,
                    message_id=mid,
                    tree_name=tree_name,
                    committed_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        s.commit()


def _rebuild_fixture(tmp_path):
    """A linux-next with three linked commits, and its first commit's
    sha (kept across the rebuild)."""
    tree_path = _make_fake_tree(
        tmp_path,
        commits=[
            (f"c{n}\n\nLink: https://lore.kernel.org/r/c{n}@x\n", b"a.c")
            for n in (1, 2, 3)
        ],
    )
    repo = Repo(str(tree_path))
    first = next(iter(repo.get_walker(include=[repo.head()], reverse=True)))
    return tree_path, first.commit.id.decode("ascii")


def test_rebuild_reader_sees_only_the_old_or_the_new_set(
    seeded_db, tmp_path, writer_thread
):
    """After every write the walk makes, the tree holds either the
    complete previous set or the complete new one. The old shape
    deleted everything first, so readers saw an empty linux-next for
    the length of the walk and lifecycle pills fell back to pending."""
    tree_path, c1_sha = _rebuild_fixture(tmp_path)
    old = {(c1_sha, "c1@x"), ("a" * 40, "gone@x")}
    _seed_rows(seeded_db, "linux-next", old)

    seen = []
    real_submit = writer_thread.submit

    def _observing_submit(op):
        future = real_submit(op)
        future.result(timeout=60)
        seen.append(_tree_keys(seeded_db, "linux-next"))
        return future

    writer_thread.submit = _observing_submit
    try:
        with seeded_db() as s:
            walk_commits(
                s,
                tree_path,
                tree_name="linux-next",
                rebases=True,
                batch_size=1,
                writer=writer_thread,
            )
    finally:
        writer_thread.submit = real_submit

    new = _tree_keys(seeded_db, "linux-next")
    assert {mid for _, mid in new} == {"c1@x", "c2@x", "c3@x"}  # precondition
    assert len(seen) >= 2, seen  # precondition: several writes observed
    assert all(state in (old, new) for state in seen), seen


def test_rebuild_that_fails_midway_leaves_rows_unchanged(
    seeded_db, tmp_path, writer_thread, monkeypatch
):
    """A walk that raises before it finishes changes nothing."""
    from mimir import mainline

    tree_path, c1_sha = _rebuild_fixture(tmp_path)
    old = {(c1_sha, "c1@x"), ("a" * 40, "gone@x")}
    _seed_rows(seeded_db, "linux-next", old)

    real_extract = mainline.extract_message_ids
    calls = []

    def _fail_on_second(message):
        calls.append(message)
        if len(calls) == 2:
            raise RuntimeError("walk interrupted")
        return real_extract(message)

    monkeypatch.setattr(mainline, "extract_message_ids", _fail_on_second)
    with seeded_db() as s, pytest.raises(RuntimeError, match="walk interrupted"):
        walk_commits(
            s,
            tree_path,
            tree_name="linux-next",
            rebases=True,
            batch_size=1,
            writer=writer_thread,
        )
    assert len(calls) == 2  # precondition: it failed mid-walk
    assert _tree_keys(seeded_db, "linux-next") == old


def test_rebuild_with_no_changes_writes_no_rows(seeded_db, tmp_path, writer_thread):
    """Re-walking an unchanged tree inserts and deletes nothing: the
    daily write volume is the day's changes, not the whole tree."""
    from sqlalchemy import text

    tree_path, _ = _rebuild_fixture(tmp_path)

    def walk():
        with seeded_db() as s:
            walk_commits(
                s,
                tree_path,
                tree_name="linux-next",
                rebases=True,
                writer=writer_thread,
            )

    walk()
    with seeded_db() as s:
        s.execute(text("CREATE TABLE IF NOT EXISTS _mc_writes (op TEXT)"))
        s.execute(
            text(
                "CREATE TRIGGER _mc_ins AFTER INSERT ON mainline_commits "
                "BEGIN INSERT INTO _mc_writes VALUES ('insert'); END"
            )
        )
        s.execute(
            text(
                "CREATE TRIGGER _mc_del AFTER DELETE ON mainline_commits "
                "BEGIN INSERT INTO _mc_writes VALUES ('delete'); END"
            )
        )
        s.commit()
    try:
        walk()
        with seeded_db() as s:
            writes = s.execute(text("SELECT op FROM _mc_writes")).scalars().all()
    finally:
        with seeded_db() as s:
            s.execute(text("DROP TRIGGER IF EXISTS _mc_ins"))
            s.execute(text("DROP TRIGGER IF EXISTS _mc_del"))
            s.execute(text("DROP TABLE IF EXISTS _mc_writes"))
            s.commit()
    assert len(_tree_keys(seeded_db, "linux-next")) == 3  # precondition
    assert writes == []


def test_rebuild_records_non_merge_commits_seen_and_linked(
    seeded_db, tmp_path, writer_thread
):
    """The linux-next rebuild stores how many non-merge commits it saw
    and how many link a message, so /merge-window can say how much of
    linux-next it can show (#703). Merges count in neither, matching
    `git rev-list --no-merges`: a merge's trailer names no patch."""
    from dulwich.objects import Commit, Tree

    repo_path = tmp_path / "next.git"
    repo = Repo.init_bare(str(repo_path), mkdir=True)
    tree = Tree()
    repo.object_store.add_object(tree)

    def commit(msg, parents):
        c = Commit()
        c.tree = tree.id
        c.parents = parents
        c.author = c.committer = b"T <t@x>"
        c.author_time = c.commit_time = 1700000000 + len(parents)
        c.author_timezone = c.commit_timezone = 0
        c.message = msg
        repo.object_store.add_object(c)
        return c.id

    a = commit(b"a\n\nLink: https://lore.kernel.org/r/a@x\n", [])
    b = commit(b"b, no trailer\n", [a])
    side = commit(b"side\n\nLink: https://lore.kernel.org/r/side@x\n", [a])
    merge = commit(b"Merge\n\nLink: https://lore.kernel.org/r/merge@x\n", [b, side])
    repo.refs[b"HEAD"] = merge

    with seeded_db() as s:
        walk_commits(
            s, repo_path, tree_name="linux-next", rebases=True, writer=writer_thread
        )
    with seeded_db() as s:
        state = s.get(MainlineState, "linux-next")
        assert (state.walked_commits, state.walked_linked) == (3, 2)


# A tree's source follows its config (#713).


def _bare_repo_with(tmp_path, name, branch, message):
    """A bare repo whose `branch` holds one commit, as a fetch source."""
    import subprocess

    work = tmp_path / f"{name}-work"
    subprocess.run(["git", "init", "-q", "-b", branch, str(work)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(work),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@x",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            message,
        ],
        check=True,
    )
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare


def _git(path, *args):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_ensure_tree_follows_a_changed_url(tmp_path):
    """Changing a tree's `url` must reach a mirror that already exists.
    A mirror fetches from the remote recorded at clone time, so the pci
    fix would otherwise never leave the config on a deployed host."""
    import subprocess

    from mimir.config import TreeConfig
    from mimir.mainline import _ensure_tree

    old = _bare_repo_with(tmp_path, "old", "main", "old source")
    new = _bare_repo_with(tmp_path, "new", "next", "new source")
    mirror = tmp_path / "pci.git"
    subprocess.run(
        ["git", "clone", "-q", "--mirror", str(old), str(mirror)], check=True
    )
    assert _git(mirror, "config", "--get", "remote.origin.url") == str(
        old
    )  # precondition

    tree = TreeConfig.model_construct(url=str(new), path=mirror, branch="next")
    _ensure_tree(tree, reference=None, skip_fetch=False)

    assert _git(mirror, "config", "--get", "remote.origin.url") == str(new)
    assert _git(mirror, "log", "-1", "--format=%s", "refs/heads/next") == "new source"


def test_ensure_tree_leaves_a_matching_url_alone(tmp_path, monkeypatch):
    """An unchanged URL rewrites nothing: no `remote set-url` per tick."""
    import subprocess

    from mimir import mainline
    from mimir.config import TreeConfig

    src = _bare_repo_with(tmp_path, "src", "main", "src")
    mirror = tmp_path / "m.git"
    subprocess.run(
        ["git", "clone", "-q", "--mirror", str(src), str(mirror)], check=True
    )
    calls = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(mainline.subprocess, "run", spy)
    tree = TreeConfig.model_construct(url=str(src), path=mirror, branch="HEAD")
    mainline._ensure_tree(tree, reference=None, skip_fetch=False)
    assert any("fetch" in c for c in calls)  # precondition: the fetch path ran
    assert not any("set-url" in c for c in calls), calls


def test_curated_pci_tree_is_the_one_linux_next_pulls():
    """linux-next's Next/Trees lists pci as `pci/pci.git#next`; HEAD of
    the old `helgaas/pci.git` is `main`, which holds nothing beyond
    Linus, so the tree produced no rows (#713)."""
    from mimir.config import _curated_default_trees

    pci = _curated_default_trees()["pci"]
    assert pci.url == "https://git.kernel.org/pub/scm/linux/kernel/git/pci/pci.git"
    assert pci.branch == "next"


def _row(commit_sha: str, message_id: str, tree_name: str = "linus"):
    return {
        "commit_sha": commit_sha,
        "message_id": message_id,
        "tree_name": tree_name,
        "committed_at": datetime(2026, 5, 30, 0, 0, 0, tzinfo=UTC),
    }


def test_submit_mainline_batch_inserts_rows_via_writer(seeded_db):
    """_submit_mainline_batch composes a WriteOp wrapping the
    INSERT OR IGNORE for a batch of (commit_sha, message_id, tree_name) rows,
    submits it to the writer, and the rows appear after the writer
    drains."""
    writer = WriterThread.from_settings()
    writer.start()
    try:
        batch = [
            _row("a" * 40, "msg-a@kernel.org"),
            _row("b" * 40, "msg-b@kernel.org"),
        ]
        future = _submit_mainline_batch(writer, "linus", batch)
        future.result(timeout=5)

        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            count = c.execute(
                text(
                    "SELECT COUNT(*) FROM mainline_commits "
                    "WHERE tree_name = 'linus' AND commit_sha IN (:a, :b)"
                ),
                {"a": "a" * 40, "b": "b" * 40},
            ).scalar_one()
            assert count == 2
            # Commit the implicit read transaction before starting the
            # cleanup DELETE; SQLAlchemy 2.0 autobegin means the SELECT
            # above already opened a transaction on this connection.
            c.commit()
            c.execute(
                text(
                    "DELETE FROM mainline_commits "
                    "WHERE tree_name = 'linus' AND commit_sha IN (:a, :b)"
                ),
                {"a": "a" * 40, "b": "b" * 40},
            )
            c.commit()
        engine.dispose()
    finally:
        writer.stop(timeout=5)


def test_submit_mainline_batch_is_idempotent_on_replay(seeded_db):
    """If a batch is submitted twice (e.g. the next tick re-walks
    after a crash), the on_conflict_do_nothing makes the second
    submission a no-op rather than a constraint error.
    Verified by re-submitting the same batch and asserting row
    count stays at the original size."""
    writer = WriterThread.from_settings()
    writer.start()
    try:
        batch = [
            _row("c" * 40, "msg-c@kernel.org"),
        ]
        _submit_mainline_batch(writer, "linus", batch).result(timeout=5)
        # Second submission: should not raise; should be a no-op.
        _submit_mainline_batch(writer, "linus", batch).result(timeout=5)

        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            count = c.execute(
                text(
                    "SELECT COUNT(*) FROM mainline_commits "
                    "WHERE tree_name = 'linus' AND commit_sha = :s"
                ),
                {"s": "c" * 40},
            ).scalar_one()
            assert count == 1
            # Commit the implicit read transaction before starting the
            # cleanup DELETE; same SQLAlchemy 2.0 autobegin reason as
            # the first test.
            c.commit()
            c.execute(
                text(
                    "DELETE FROM mainline_commits "
                    "WHERE tree_name = 'linus' AND commit_sha = :s"
                ),
                {"s": "c" * 40},
            )
            c.commit()
        engine.dispose()
    finally:
        writer.stop(timeout=5)


def test_submit_mainline_batch_empty_batch_is_noop(seeded_db):
    """Empty batch returns a pre-resolved future without submitting
    anything to the writer (so the writer doesn't get spurious
    no-op WriteOps that just waste a transaction)."""
    writer = WriterThread.from_settings()
    writer.start()
    try:
        future = _submit_mainline_batch(writer, "linus", [])
        # Future should resolve immediately; .result() with timeout=0
        # should work (would block if a real submit happened).
        assert future.done()
        assert future.result() is None
    finally:
        writer.stop(timeout=5)


def test_submit_mainline_cursor_update_advances_state_via_writer(seeded_db):
    """_submit_mainline_cursor_update composes a WriteOp wrapping the
    MainlineState upsert for a tree's commits_walked_to_sha cursor,
    submits it to the writer, and the state row reflects the new
    SHA after the writer drains.

    UPSERT semantics: works whether the row exists or not.
    """
    from sqlalchemy import create_engine, text

    from mimir.broker.writes import WriterThread
    from mimir.config import settings
    from mimir.mainline import _submit_mainline_cursor_update

    writer = WriterThread.from_settings()
    writer.start()
    try:
        # First update: row may or may not exist for tree "linus"
        # from a prior test; either way the upsert should leave the
        # cursor at "d" * 40.
        future = _submit_mainline_cursor_update(writer, "linus", "d" * 40)
        future.result(timeout=5)

        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            sha = c.execute(
                text(
                    "SELECT commits_walked_to_sha FROM mainline_state "
                    "WHERE tree_name = 'linus'"
                )
            ).scalar_one_or_none()
            assert sha == "d" * 40
            c.commit()

            # Reset for other tests: set cursor back to NULL.
            c.execute(
                text(
                    "UPDATE mainline_state SET commits_walked_to_sha = NULL "
                    "WHERE tree_name = 'linus'"
                )
            )
            c.commit()
        engine.dispose()
    finally:
        writer.stop(timeout=5)


def test_submit_mainline_cursor_update_creates_row_when_missing(seeded_db):
    """If MainlineState has no row for this tree yet, the upsert
    creates one with the cursor set. Covers the "new tree" boot
    case where MainlineState may be empty."""
    from sqlalchemy import create_engine, text

    from mimir.broker.writes import WriterThread
    from mimir.config import settings
    from mimir.mainline import _submit_mainline_cursor_update

    writer = WriterThread.from_settings()
    writer.start()
    engine = create_engine(settings.database_url, future=True)
    try:
        # Ensure no row exists for tree "phase3-new".
        with engine.connect() as c:
            c.execute(text("DELETE FROM mainline_state WHERE tree_name = 'phase3-new'"))
            c.commit()

        future = _submit_mainline_cursor_update(writer, "phase3-new", "e" * 40)
        future.result(timeout=5)

        with engine.connect() as c:
            sha = c.execute(
                text(
                    "SELECT commits_walked_to_sha FROM mainline_state "
                    "WHERE tree_name = 'phase3-new'"
                )
            ).scalar_one()
            assert sha == "e" * 40
            c.commit()

            # Cleanup.
            c.execute(text("DELETE FROM mainline_state WHERE tree_name = 'phase3-new'"))
            c.commit()
    finally:
        engine.dispose()
        writer.stop(timeout=5)


def test_mid_walk_cursor_failure_leaves_cursor_at_old_position(
    seeded_db, tmp_path, monkeypatch
):
    """Phase 3 contract: a crash between batch N and the cursor
    submission leaves the cursor at its old position, so the next
    tick re-walks from there. on_conflict_do_nothing on
    (commit_sha, message_id, tree_name) makes that replay a no-op for the
    batches that committed pre-crash.

    The test injects a failing _submit_mainline_cursor_update,
    asserts that batches committed but the cursor stayed at NULL,
    then replays the walk for real and asserts no duplicate rows
    and the cursor lands at HEAD."""
    from concurrent.futures import Future

    import sqlalchemy as sa
    from dulwich.repo import Repo
    from sqlalchemy import create_engine

    import mimir.mainline as mainline_mod
    from mimir.broker.writes import WriterThread
    from mimir.config import settings
    from mimir.mainline import walk_commits
    from mimir.models import MainlineCommit, MainlineState

    # Build a small bare repo with 5 commits that all carry Link: trailers.
    # batch_size=2 gives us 2 full batches (commits 1+2, commits 3+4) and
    # one tail batch (commit 5). The cursor update fires after the tail
    # batch (the injection point).
    repo_path = tmp_path / "crash-test.git"
    repo = Repo.init_bare(str(repo_path), mkdir=True)

    prev = None
    for i in range(1, 6):
        prev = _build_commit(
            repo,
            f"Fix {i}.\n\nLink: https://lore.kernel.org/r/m{i}@test\n".encode(),
            parent=prev,
        )

    tree_name = "crash-test"

    writer = WriterThread.from_settings()
    writer.start()
    try:
        # Walk 1: inject a failing cursor helper. The batch WriteOps
        # have already committed before the cursor fires (per Phase 3's
        # "cursor is the final WriteOp" design), so some MainlineCommit
        # rows should be present after the exception propagates.
        def _failing_cursor(w, tn, last_sha):
            f: Future = Future()
            f.set_exception(RuntimeError("simulated crash before cursor commit"))
            return f

        monkeypatch.setattr(
            mainline_mod, "_submit_mainline_cursor_update", _failing_cursor
        )

        import pytest as _pytest

        with _pytest.raises(RuntimeError, match="simulated crash"):
            with seeded_db() as session:
                walk_commits(
                    session,
                    repo_path,
                    tree_name=tree_name,
                    writer=writer,
                    batch_size=2,
                    branch="HEAD",
                )

        # State A: some MainlineCommit rows committed (the batches before
        # the cursor failed), but cursor still at the pre-walk value (NULL).
        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            rows_after_crash = c.execute(
                sa.select(MainlineCommit.commit_sha).where(
                    MainlineCommit.tree_name == tree_name
                )
            ).all()
            state_after_crash = c.execute(
                sa.select(MainlineState.commits_walked_to_sha).where(
                    MainlineState.tree_name == tree_name
                )
            ).scalar_one_or_none()
            c.commit()

        assert len(rows_after_crash) > 0, (
            "some batches should have committed before the cursor failure"
        )
        assert state_after_crash is None, (
            "cursor must NOT have advanced if the final WriteOp failed"
        )

        # Walk 2: restore the real cursor helper and replay. The already-
        # committed MainlineCommit rows are absorbed by on_conflict_do_nothing;
        # the cursor should advance to HEAD.
        monkeypatch.undo()

        with seeded_db() as session:
            walk_commits(
                session,
                repo_path,
                tree_name=tree_name,
                writer=writer,
                batch_size=2,
                branch="HEAD",
            )

        with engine.connect() as c:
            rows_after_replay = c.execute(
                sa.select(MainlineCommit.commit_sha).where(
                    MainlineCommit.tree_name == tree_name
                )
            ).all()
            state_after_replay = c.execute(
                sa.select(MainlineState.commits_walked_to_sha).where(
                    MainlineState.tree_name == tree_name
                )
            ).scalar_one_or_none()
            c.commit()

        # No duplicates: on_conflict_do_nothing absorbed the already-
        # committed batches. The cursor advanced to HEAD.
        assert len(rows_after_replay) == 5, (
            f"expected exactly 5 rows (one per Link: commit), got {len(rows_after_replay)}"
        )
        assert state_after_replay is not None, (
            "cursor should have advanced on the successful replay"
        )

        # Cleanup so other tests start clean.
        with engine.connect() as c:
            c.execute(
                sa.delete(MainlineCommit).where(MainlineCommit.tree_name == tree_name)
            )
            c.execute(
                sa.delete(MainlineState).where(MainlineState.tree_name == tree_name)
            )
            c.commit()
        engine.dispose()
    finally:
        writer.stop(timeout=10)


def test_update_mainline_uses_writer_thread_via_active_context(
    seeded_db, tmp_path, monkeypatch
):
    """update_mainline dispatches its writes through the active writer
    thread. write_transaction was removed in Phase 6b; the behavioral
    assertion here pins that update_mainline runs to completion via
    the writer path without error.

    Scoped to ONE local tree via `linus_tree`. It previously ran against
    the real seven-tree `settings.trees`, which made its meaning
    machine-dependent: with `Mainline/*.git` present it exercised the
    writer path for real, and without them it cloned seven kernel trees.
    Once the `_no_surprise_clone` guard landed, the same test skipped
    every tree and still passed, asserting only `result is not None`
    while reaching neither `_ensure_tree`'s successor code nor the
    writer dispatch it exists to pin.
    """
    from mimir.broker import _context
    from mimir.broker.pools import ReadSessionPool
    from mimir.broker.writes import WriterThread
    from mimir.config import settings
    from tests.conftest import linus_tree

    repo = tmp_path / "linus.git"
    subprocess.run(["git", "init", "--bare", "-q", str(repo)], check=True)
    monkeypatch.setattr(settings, "trees", linus_tree(repo))

    pool = ReadSessionPool.from_settings()
    writer = WriterThread.from_settings()
    writer.start()

    try:
        _context.set_active(pool, writer)
        from mimir.mainline import update_mainline

        # skip_fetch=True: no subprocess.run (no clone/fetch).
        # skip_maintainers=True: load_maintainers never runs.
        # skip_commits=True: walk_commits never runs, but
        # update_mainline's per-tree iteration still executes.
        result = update_mainline(
            skip_fetch=True,
            skip_maintainers=True,
            skip_commits=True,
        )
        assert result is not None
    finally:
        _context.clear_active()
        writer.stop(timeout=10)
        pool.close()


def test_walk_commits_rebases_dispatches_rebuild_then_cursor(seeded_db, tmp_path):
    """`rebases=True` trees (linux-next, mm/mm-stable, etc.) must
    dispatch exactly two WriteOps, in this order:

      1. `mainline:<tree>:rebuild`: the whole walk applied by
         difference in one transaction (#671), so readers never see a
         partial tree and a failed walk changes nothing;
      2. `mainline:<tree>:cursor`: the FINAL WriteOp, as for every
         tree, so a crash before it leaves the cursor where it was.

    This test pins the SUBMIT SEQUENCE by recording every
    `writer.submit()` call's label; the rows assertion pins that the
    one rebuild carried the whole walk.
    """
    import sqlalchemy as sa
    from dulwich.repo import Repo
    from sqlalchemy import create_engine

    from mimir.broker.writes import WriterThread
    from mimir.config import settings
    from mimir.mainline import walk_commits
    from mimir.models import MainlineCommit, MainlineState

    repo_path = tmp_path / "rebases-test.git"
    repo = Repo.init_bare(str(repo_path), mkdir=True)

    # 3 Link-trailered commits; batch_size=2 would have given 2 batches
    # in the old shape. Now: 1 rebuild + 1 cursor, whatever batch_size.
    prev = None
    for i in range(1, 4):
        prev = _build_commit(
            repo,
            f"Fix {i}.\n\nLink: https://lore.kernel.org/r/m{i}@test\n".encode(),
            parent=prev,
        )

    tree_name = "rebases-sequence-test"

    writer = WriterThread.from_settings()
    writer.start()
    try:
        real_submit = writer.submit
        captured_labels: list[str] = []

        def _recording_submit(op):
            captured_labels.append(op.label)
            return real_submit(op)

        writer.submit = _recording_submit  # type: ignore[method-assign]
        try:
            with seeded_db() as session:
                walk_commits(
                    session,
                    repo_path,
                    tree_name=tree_name,
                    writer=writer,
                    batch_size=2,
                    branch="HEAD",
                    rebases=True,
                )
        finally:
            writer.submit = real_submit  # type: ignore[method-assign]

        # Filter to labels for THIS tree only (the session broker may
        # have unrelated WriteOps interleaved on its writer).
        tree_labels = [
            label
            for label in captured_labels
            if label.startswith(f"mainline:{tree_name}:")
        ]

        # One rebuild, then the cursor LAST. No pre-walk DELETE and no
        # per-batch inserts: either exposes a partial tree to readers
        # between commits (#671).
        assert tree_labels == [
            f"mainline:{tree_name}:rebuild",
            f"mainline:{tree_name}:cursor",
        ], tree_labels
        with seeded_db() as session:
            mids = set(
                session.execute(
                    sa.select(MainlineCommit.message_id).where(
                        MainlineCommit.tree_name == tree_name
                    )
                ).scalars()
            )
        assert mids == {"m1@test", "m2@test", "m3@test"}

        # Cleanup so the synthetic tree_name doesn't pollute subsequent tests.
        engine = create_engine(settings.database_url, future=True)
        with engine.connect() as c:
            c.execute(
                sa.delete(MainlineCommit).where(MainlineCommit.tree_name == tree_name)
            )
            c.execute(
                sa.delete(MainlineState).where(MainlineState.tree_name == tree_name)
            )
            c.commit()
        engine.dispose()
    finally:
        writer.stop(timeout=10)


def test_cache_writes_drain_between_mainline_batches(seeded_db, tmp_path):
    """Phase 3 contract: while walk_commits dispatches batches through
    the writer, concurrent cache-set writes drain between batches
    instead of head-of-line-stalling for the full walk.

    Pre-Phase 3 held the writer lock in one continuous transaction for
    the entire walk. Phase 3 turns that into N short per-batch
    transactions, so cache.set-shaped WriteOps submitted from other
    threads see only one batch worth of lock hold, not the whole walk.

    Asserts: max per-cache-write wall time < 5 s (generous; on a
    healthy laptop the real numbers are tens of ms). The point is the
    absence of head-of-line stalls, not a wall-time speedup."""
    from dulwich.repo import Repo

    from mimir.mainline import walk_commits

    # --- build a 20-commit bare repo with Link: trailers -----------------
    # batch_size=5 gives 4 batches of 5 commits each.  Each batch's
    # WriteOp commits independently, releasing the writer lock before the
    # next batch starts.  The gap between batches is where concurrent
    # cache-set WriteOps can drain.
    repo_path = tmp_path / "ft-stress.git"
    repo = Repo.init_bare(str(repo_path), mkdir=True)

    prev = None
    for idx in range(1, 21):
        prev = _build_commit(
            repo,
            f"Fix {idx}.\n\nLink: https://lore.kernel.org/r/ft{idx}@test\n".encode(),
            parent=prev,
        )

    tree_name = "ft-stress"

    # --- set up WriterThread and read session ----------------------------
    writer = WriterThread.from_settings()
    writer.start()

    # A writable engine for cleanup; reads come from seeded_db sessions.
    engine = create_engine(settings.database_url, future=True)

    cache_durations: list[float] = []
    cache_errors: list[Exception] = []

    def _cache_set_fn(conn, key: str) -> None:
        # Mimic the shape of a cache UPSERT: a single INSERT OR REPLACE
        # into the cache table.  expires_at is set to a far-future unix
        # timestamp so the row doesn't collide with real cache logic.
        conn.execute(
            text(
                "INSERT OR REPLACE INTO cache (key, value, expires_at) "
                "VALUES (:k, :v, :e)"
            ),
            {"k": key, "v": '{"x": 1}', "e": 9999999999},
        )

    def _submit_cache(i: int) -> None:
        key = f"phase3_ft_stress_{i}"
        t0 = time.perf_counter()
        try:
            writer.submit(
                WriteOp(
                    label=f"cache:set:{key}",
                    fn=functools.partial(_cache_set_fn, key=key),
                )
            ).result(timeout=10)
        except Exception as exc:
            cache_errors.append(exc)
        cache_durations.append(time.perf_counter() - t0)

    # --- drive the walk in a background thread ---------------------------
    walk_done = threading.Event()
    walk_exc: list[Exception] = []

    def _walk() -> None:
        try:
            with seeded_db() as session:
                walk_commits(
                    session,
                    repo_path,
                    tree_name=tree_name,
                    writer=writer,
                    batch_size=5,
                    branch="HEAD",
                )
        except Exception as exc:
            walk_exc.append(exc)
        finally:
            walk_done.set()

    walk_thread = threading.Thread(target=_walk, daemon=True)
    walk_thread.start()

    # --- fire 50 cache-set WriteOps from a thread pool -------------------
    # The pool submits each write as soon as a worker is free.  The 8
    # workers compete for the writer queue alongside the walk's batches.
    with ThreadPoolExecutor(max_workers=8) as pool_executor:
        for i in range(50):
            pool_executor.submit(_submit_cache, i)
    # All 50 .result() calls have returned by here (pool_executor.__exit__
    # joins all futures before leaving the block).

    walk_done.wait(timeout=60)

    # --- cleanup ---------------------------------------------------------
    # Remove the test rows regardless of assertion outcome so other tests
    # start with a clean state.
    try:
        with engine.connect() as c:
            c.execute(
                text("DELETE FROM mainline_commits WHERE tree_name = :t"),
                {"t": tree_name},
            )
            c.execute(
                text("DELETE FROM mainline_state WHERE tree_name = :t"),
                {"t": tree_name},
            )
            # Remove the synthetic cache rows inserted by the test.
            for i in range(50):
                c.execute(
                    text("DELETE FROM cache WHERE key = :k"),
                    {"k": f"phase3_ft_stress_{i}"},
                )
            c.commit()
    finally:
        engine.dispose()
        writer.stop(timeout=10)

    # --- assertions ------------------------------------------------------
    assert not walk_exc, f"walk_commits failed: {walk_exc[0]!r}"
    assert not cache_errors, f"cache WriteOps errored: {cache_errors}"
    assert cache_durations, "no cache durations collected"

    max_latency = max(cache_durations)
    assert max_latency < 5.0, (
        f"cache.set tail latency was {max_latency:.2f} s. Phase 3 "
        "promises bounded tails (well under the ~62 s pre-Phase-3 "
        "stall); a >5 s tail suggests the walker is holding the "
        "writer lock across batches instead of committing per batch."
    )
