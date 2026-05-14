"""Tests for `mimir.mainline` — Link-trailer extraction +
the dulwich-driven commit walker.

Real-world Link trailers (sampled from the upstream Linus tree) are
included as parser fixtures; the walker tests use fake bare repos
built with dulwich so the suite stays offline + fast.
"""
from datetime import datetime, timezone
from pathlib import Path

from dulwich.objects import Commit, Tree
from dulwich.repo import Repo
from sqlalchemy import select

from mimir.mainline import extract_message_ids, walk_commits
from mimir.models import MainlineCommit, MainlineState


# extract_message_ids — pure function, no DB.


def test_extract_short_form_link():
    """`Link: https://lore.kernel.org/r/<msgid>` is the canonical
    short form most commits use."""
    msg = b"Subject line.\n\nLink: https://lore.kernel.org/r/aNU-FkJEcA3T4aDB@intel.com\n"
    assert extract_message_ids(msg) == ["aNU-FkJEcA3T4aDB@intel.com"]


def test_extract_no_slug():
    """Older / hand-crafted commits drop the slug entirely.
    `Link: https://lore.kernel.org/<msgid>`."""
    msg = b"Link: https://lore.kernel.org/175852292275.307379@devnote2\n"
    assert extract_message_ids(msg) == ["175852292275.307379@devnote2"]


def test_extract_with_list_slug_and_trailing_slash():
    """`Link: https://lore.kernel.org/all/<msgid>/` (list-named
    slug, trailing slash) — common when the committer pasted the
    URL from a browser."""
    msg = b"Link: https://lore.kernel.org/all/175824455687.45175.3734166065458520748.stgit@devnote2/\n"
    assert extract_message_ids(msg) == [
        "175824455687.45175.3734166065458520748.stgit@devnote2",
    ]


def test_extract_multiple_links():
    """A commit referencing multiple lore threads — rare but
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
    Those reference issues, not lore msgids — must be ignored so
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


def test_extract_handles_non_decodable_bytes_via_surrogateescape():
    """A stray non-UTF-8 byte must not crash the extractor — those
    appear occasionally in older commits with contributor names
    in legacy encodings."""
    # 0xff is invalid UTF-8 but the regex doesn't care about that
    # region of the message; the Link line still matches.
    msg = b"Co-developed-by: Jos\xff Doe <j@x>\nLink: https://lore.kernel.org/r/keep@me\n"
    assert extract_message_ids(msg) == ["keep@me"]


def test_extract_quoted_link_in_body_still_matches():
    """The MULTILINE anchor is `^Link:` — quoted variants like
    `> Link:` don't match (good; that's a re-quote, not the
    commit's own trailer)."""
    msg = b"Subject.\n\n> Link: https://lore.kernel.org/r/not-mine@x\nLink: https://lore.kernel.org/r/mine@x\n"
    assert extract_message_ids(msg) == ["mine@x"]


# walk_commits — integration with dulwich + DB.


def _build_commit(
    repo: Repo, message: bytes, parent: bytes | None = None,
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
    seeded_db, tmp_path,
):
    repo = _bare_repo(tmp_path / "tree.git")
    c1 = _build_commit(repo, b"Fix something.\n\nLink: https://lore.kernel.org/r/m1@x\n")
    _build_commit(
        repo,
        b"Fix something else.\n\nLink: https://lore.kernel.org/r/m2@x\nLink: https://lore.kernel.org/r/m3@x\n",
        parent=c1,
    )
    with seeded_db() as s:
        result = walk_commits(s, tmp_path / "tree.git")

    assert result.commits_seen == 2
    assert result.linked == 2
    assert result.rows_inserted == 3

    with seeded_db() as s:
        rows = {
            (r.message_id, r.tree_name) for r in
            s.execute(select(MainlineCommit)).scalars()
        }
    assert rows == {("m1@x", "linus"), ("m2@x", "linus"), ("m3@x", "linus")}


def test_walk_commits_resumes_from_cursor(seeded_db, tmp_path):
    """Second walk only sees the commits added since the prior
    cursor — that's the steady-state cheap path."""
    repo_path = tmp_path / "tree.git"
    repo = _bare_repo(repo_path)
    c1 = _build_commit(repo, b"first\n\nLink: https://lore.kernel.org/r/m1@x\n")

    with seeded_db() as s:
        result_1 = walk_commits(s, repo_path)
    assert result_1.commits_seen == 1
    assert result_1.rows_inserted == 1

    # Append a second commit then walk again.
    _build_commit(repo, b"second\n\nLink: https://lore.kernel.org/r/m2@x\n", parent=c1)
    with seeded_db() as s:
        result_2 = walk_commits(s, repo_path)
    assert result_2.commits_seen == 1   # only the new one
    assert result_2.rows_inserted == 1

    with seeded_db() as s:
        rows = {r.message_id for r in s.execute(select(MainlineCommit)).scalars()}
    assert rows == {"m1@x", "m2@x"}


def test_walk_commits_noop_when_head_unchanged(seeded_db, tmp_path):
    """Walking the same head twice in a row examines zero commits."""
    repo = _bare_repo(tmp_path / "tree.git")
    _build_commit(repo, b"only\n\nLink: https://lore.kernel.org/r/m@x\n")
    with seeded_db() as s:
        walk_commits(s, tmp_path / "tree.git")
    with seeded_db() as s:
        result = walk_commits(s, tmp_path / "tree.git")
    assert result.commits_seen == 0
    assert result.rows_inserted == 0


def test_walk_commits_skips_commits_without_link(seeded_db, tmp_path):
    """Most kernel commits don't carry a lore `Link:` — those
    consume a `commits_seen` slot but produce no rows. Pins the
    counter shape so the operator can see "walked N, linked K"
    progress."""
    repo = _bare_repo(tmp_path / "tree.git")
    c1 = _build_commit(repo, b"no lore link here\n\nSigned-off-by: X <x@y>\n")
    _build_commit(repo, b"another\n\nLink: https://github.com/x/y/issues/1\n", parent=c1)

    with seeded_db() as s:
        result = walk_commits(s, tmp_path / "tree.git")
    assert result.commits_seen == 2
    assert result.linked == 0
    assert result.rows_inserted == 0


def test_walk_commits_records_commit_time(seeded_db, tmp_path):
    """`committed_at` carries the commit's timestamp — that's what
    the patch-page surface renders as "on <date>"."""
    repo = _bare_repo(tmp_path / "tree.git")
    # 2024-06-01 00:00:00 UTC
    ts = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp())
    _build_commit(
        repo, b"x\n\nLink: https://lore.kernel.org/r/m@x\n",
        commit_time=ts,
    )
    with seeded_db() as s:
        walk_commits(s, tmp_path / "tree.git")
    with seeded_db() as s:
        row = s.execute(select(MainlineCommit)).scalar_one()
    # SQLite stores datetimes without tzinfo on round-trip; the
    # value is naive UTC by convention (same shape as
    # `Article.date`, see CONTEXT.md "tz-aware UTC normalization").
    # Render code attaches UTC at the consumer end.
    assert row.committed_at.replace(tzinfo=timezone.utc) == datetime(
        2024, 6, 1, tzinfo=timezone.utc,
    )


def test_walk_commits_advances_state_cursor(seeded_db, tmp_path):
    """After a walk, `MainlineState.commits_walked_to_sha` points
    at the most recent commit on HEAD. Otherwise the next walk
    would re-do work."""
    repo = _bare_repo(tmp_path / "tree.git")
    _build_commit(repo, b"x\n\nLink: https://lore.kernel.org/r/m@x\n")
    head_sha = repo.head().decode("ascii")
    with seeded_db() as s:
        walk_commits(s, tmp_path / "tree.git")
    with seeded_db() as s:
        state = s.get(MainlineState, "linus")
    assert state is not None
    assert state.commits_walked_to_sha == head_sha


def test_walk_commits_rewalks_when_cursor_missing(seeded_db, tmp_path):
    """If the cursor points at a SHA no longer in the repo (history
    rewrite, shallow re-clone), the walker re-walks from scratch
    rather than crashing. Defensive — Linus's tree shouldn't
    history-rewrite, but stable trees occasionally do."""
    repo = _bare_repo(tmp_path / "tree.git")
    _build_commit(repo, b"x\n\nLink: https://lore.kernel.org/r/m@x\n")

    # Seed the state with a SHA that doesn't exist.
    with seeded_db() as s:
        s.add(MainlineState(
            tree_name="linus", commits_walked_to_sha="0" * 40,
        ))
        s.commit()
    with seeded_db() as s:
        result = walk_commits(s, tmp_path / "tree.git")
    # Walker re-walks from the beginning despite the stale cursor.
    assert result.commits_seen == 1
    assert result.rows_inserted == 1
