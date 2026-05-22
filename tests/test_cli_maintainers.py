"""CLI integration for `update-mainline`. The parser is exercised
in unit-isolation by `tests/test_maintainers.py`; this file pins the
clone/fetch/load wiring against a fake bare repo.
"""

from pathlib import Path

from click.testing import CliRunner
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo
from sqlalchemy import select

from mimir.cli import update_mainline_command
from mimir.config import settings
from mimir.models import MainlineState, Subsystem


_SAMPLE_MAINTAINERS = (
    b"List of maintainers and how to submit kernel changes\n"
    b"====================================================\n"
    b"\n"
    b"Description of section entries:\n"
    b"\n"
    b"    M: Mail patches to: FullName <address@domain>\n"
    b"\n"
    b"Maintainers List\n"
    b"----------------\n"
    b"\n"
    b"BCACHEFS\n"
    b"M:\tKent Overstreet <kent.overstreet@linux.dev>\n"
    b"L:\tlinux-bcachefs@vger.kernel.org\n"
    b"S:\tMaintained\n"
    b"F:\tfs/bcachefs/\n"
    b"\n"
    b"BTRFS\n"
    b"M:\tChris Mason <clm@example>\n"
    b"M:\tJosef Bacik <josef@example>\n"
    b"S:\tMaintained\n"
    b"F:\tfs/btrfs/\n"
    b"X:\tfs/btrfs/tests/\n"
)


def _build_mainline_repo(
    repo_path: Path, blob_bytes: bytes, parent: bytes | None = None
) -> bytes:
    """Build (or append to) a bare repo with a single `MAINTAINERS`
    blob at HEAD. Returns the commit id. If `parent` is supplied,
    the new commit chains onto it, useful for testing "HEAD moved"
    flow."""
    repo = (
        Repo(str(repo_path))
        if repo_path.exists()
        else Repo.init_bare(str(repo_path), mkdir=True)
    )
    blob = Blob.from_string(blob_bytes)
    repo.object_store.add_object(blob)
    tree = Tree()
    tree.add(b"MAINTAINERS", 0o100644, blob.id)
    repo.object_store.add_object(tree)
    commit = Commit()
    commit.tree = tree.id
    commit.parents = [parent] if parent else []
    commit.author = commit.committer = b"test <t@x>"
    commit.commit_time = commit.author_time = 1700000000
    commit.commit_timezone = commit.author_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = b"load MAINTAINERS"
    repo.object_store.add_object(commit)
    repo.refs[b"HEAD"] = commit.id
    return commit.id


def test_update_mainline_loads_subsystems_from_head_blob(
    seeded_db,
    tmp_path,
    monkeypatch,
):
    """Happy path: bare repo with a MAINTAINERS blob → CLI reads it,
    parses, and inserts rows. Pins the wire shape: subsystems table
    matches the parsed entries; MainlineState records the HEAD."""
    repo_path = tmp_path / "linux.git"
    head_sha = _build_mainline_repo(repo_path, _SAMPLE_MAINTAINERS)
    monkeypatch.setattr(settings, "mainline_tree_path", repo_path)

    # --skip-fetch: the repo is local, no upstream to pull from.
    result = CliRunner().invoke(update_mainline_command, ["--skip-fetch"])
    assert result.exit_code == 0, result.output
    assert "loaded 2 subsystems" in result.output
    assert "linus@" in result.output

    with seeded_db() as s:
        subs = list(s.execute(select(Subsystem).order_by(Subsystem.name)).scalars())
        names = [sub.name for sub in subs]
        assert names == ["BCACHEFS", "BTRFS"]

        bcachefs = next(sub for sub in subs if sub.name == "BCACHEFS")
        assert bcachefs.status == "Maintained"
        assert [p.glob for p in bcachefs.paths] == ["fs/bcachefs/"]
        assert [(m.role, m.address) for m in bcachefs.maintainers] == [
            ("M", "kent.overstreet@linux.dev"),
        ]

        btrfs = next(sub for sub in subs if sub.name == "BTRFS")
        assert {p.glob: p.is_exclude for p in btrfs.paths} == {
            "fs/btrfs/": False,
            "fs/btrfs/tests/": True,
        }
        assert len(btrfs.maintainers) == 2

        state = s.get(MainlineState, "linus")
        assert state is not None
        assert state.last_commit_sha == head_sha.decode("ascii")


def test_update_mainline_is_noop_when_head_unchanged(
    seeded_db,
    tmp_path,
    monkeypatch,
):
    """Second tick on an unchanged HEAD must skip the parse step
    that's the steady-state cheap path."""
    repo_path = tmp_path / "linux.git"
    _build_mainline_repo(repo_path, _SAMPLE_MAINTAINERS)
    monkeypatch.setattr(settings, "mainline_tree_path", repo_path)

    runner = CliRunner()
    assert runner.invoke(update_mainline_command, ["--skip-fetch"]).exit_code == 0
    # Second invocation: same HEAD → no-op message for MAINTAINERS,
    # no work done on that side. (The Link-trailer walker still
    # runs but examines zero new commits.)
    result = runner.invoke(update_mainline_command, ["--skip-fetch"])
    assert result.exit_code == 0
    assert "MAINTAINERS unchanged" in result.output
    # Row count unchanged (sanity: the no-op didn't accidentally
    # truncate and not re-insert).
    with seeded_db() as s:
        assert s.execute(select(Subsystem)).all()


def test_update_mainline_force_reparses_even_on_unchanged_head(
    seeded_db,
    tmp_path,
    monkeypatch,
):
    """`--force` re-parses regardless of HEAD state. Useful after a
    parser fix or for debugging, operator wants the side effect
    even when the cheap path would skip."""
    repo_path = tmp_path / "linux.git"
    _build_mainline_repo(repo_path, _SAMPLE_MAINTAINERS)
    monkeypatch.setattr(settings, "mainline_tree_path", repo_path)

    runner = CliRunner()
    assert runner.invoke(update_mainline_command, ["--skip-fetch"]).exit_code == 0
    result = runner.invoke(update_mainline_command, ["--skip-fetch", "--force"])
    assert result.exit_code == 0
    assert "loaded 2 subsystems" in result.output


def test_update_mainline_picks_up_head_movement(
    seeded_db,
    tmp_path,
    monkeypatch,
):
    """A new commit on the bare repo with a different MAINTAINERS
    blob → next tick must pick up the new content. Pins the "tree
    is the source of truth" replace-all behaviour: the now-removed
    BTRFS section is gone, the new XFS section landed."""
    repo_path = tmp_path / "linux.git"
    first = _build_mainline_repo(repo_path, _SAMPLE_MAINTAINERS)
    monkeypatch.setattr(settings, "mainline_tree_path", repo_path)

    runner = CliRunner()
    runner.invoke(update_mainline_command, ["--skip-fetch"])

    updated_blob = (
        b"List of maintainers and how to submit kernel changes\n"
        b"====================================================\n"
        b"\n"
        b"Description of section entries:\n"
        b"\n"
        b"Maintainers List\n"
        b"----------------\n"
        b"\n"
        b"BCACHEFS\n"
        b"M:\tKent Overstreet <kent.overstreet@linux.dev>\n"
        b"S:\tMaintained\n"
        b"F:\tfs/bcachefs/\n"
        b"\n"
        b"XFS FILESYSTEM\n"
        b"M:\tDarrick J. Wong <djwong@kernel.org>\n"
        b"S:\tSupported\n"
        b"F:\tfs/xfs/\n"
    )
    _build_mainline_repo(repo_path, updated_blob, parent=first)

    result = runner.invoke(update_mainline_command, ["--skip-fetch"])
    assert result.exit_code == 0
    assert "loaded 2 subsystems" in result.output

    with seeded_db() as s:
        names = {sub.name for sub in s.execute(select(Subsystem)).scalars()}
        assert names == {"BCACHEFS", "XFS FILESYSTEM"}, (
            "old subsystems must be gone after replace-all"
        )


def test_update_mainline_walks_link_trailers_in_commit_messages(
    seeded_db,
    tmp_path,
    monkeypatch,
):
    """The MAINTAINERS load and the Link-trailer walk are
    independent passes on the same tree. The walker indexes
    every `Link: https://lore.kernel.org/.../<msgid>` it finds
    into `mainline_commits`. Pins the second-pass wiring inside
    `update-mainline`."""
    repo_path = tmp_path / "linux.git"
    _build_mainline_repo(repo_path, _SAMPLE_MAINTAINERS)
    # Append a commit with a lore Link trailer on top of the
    # MAINTAINERS commit. The walker should see two commits and
    # extract one row.
    from dulwich.repo import Repo
    from dulwich.objects import Blob, Commit, Tree

    repo = Repo(str(repo_path))
    parent = repo.head()
    blob = Blob.from_string(_SAMPLE_MAINTAINERS)
    repo.object_store.add_object(blob)
    tree = Tree()
    tree.add(b"MAINTAINERS", 0o100644, blob.id)
    repo.object_store.add_object(tree)
    extra = Commit()
    extra.tree = tree.id
    extra.parents = [parent]
    extra.author = extra.committer = b"t <t@x>"
    extra.commit_time = extra.author_time = 1700000100
    extra.commit_timezone = extra.author_timezone = 0
    extra.encoding = b"UTF-8"
    extra.message = (
        b"Fix something.\n\nLink: https://lore.kernel.org/r/fix-msg@example.com\n"
    )
    repo.object_store.add_object(extra)
    repo.refs[b"HEAD"] = extra.id
    monkeypatch.setattr(settings, "mainline_tree_path", repo_path)

    result = CliRunner().invoke(update_mainline_command, ["--skip-fetch"])
    assert result.exit_code == 0, result.output
    assert "walked 2 commits" in result.output

    from mimir.models import MainlineCommit

    with seeded_db() as s:
        rows = list(s.execute(select(MainlineCommit)).scalars())
    assert [r.message_id for r in rows] == ["fix-msg@example.com"]


def test_update_mainline_skip_commits_does_not_walk(
    seeded_db,
    tmp_path,
    monkeypatch,
):
    """`--skip-commits` keeps the MAINTAINERS pass but skips the
    Link-trailer walk. Useful when an operator only wants to
    refresh the subsystem schema."""
    repo_path = tmp_path / "linux.git"
    _build_mainline_repo(repo_path, _SAMPLE_MAINTAINERS)
    monkeypatch.setattr(settings, "mainline_tree_path", repo_path)
    result = CliRunner().invoke(
        update_mainline_command,
        ["--skip-fetch", "--skip-commits"],
    )
    assert result.exit_code == 0
    assert "loaded 2 subsystems" in result.output
    assert "walked" not in result.output

    from mimir.models import MainlineCommit

    with seeded_db() as s:
        assert s.execute(select(MainlineCommit)).all() == []


def test_update_mainline_skip_maintainers_only_walks_commits(
    seeded_db,
    tmp_path,
    monkeypatch,
):
    """`--skip-maintainers` does the inverse, only the
    Link-trailer walk runs. Useful for an operator who wants to
    backfill mainline_commits without re-loading subsystems."""
    repo_path = tmp_path / "linux.git"
    _build_mainline_repo(
        repo_path,
        _SAMPLE_MAINTAINERS,
    )
    # Hand-build a commit with a Link trailer on top.
    from dulwich.repo import Repo
    from dulwich.objects import Blob, Commit, Tree

    repo = Repo(str(repo_path))
    parent = repo.head()
    blob = Blob.from_string(_SAMPLE_MAINTAINERS)
    repo.object_store.add_object(blob)
    tree = Tree()
    tree.add(b"MAINTAINERS", 0o100644, blob.id)
    repo.object_store.add_object(tree)
    extra = Commit()
    extra.tree = tree.id
    extra.parents = [parent]
    extra.author = extra.committer = b"t <t@x>"
    extra.commit_time = extra.author_time = 1700000100
    extra.commit_timezone = extra.author_timezone = 0
    extra.encoding = b"UTF-8"
    extra.message = b"x\n\nLink: https://lore.kernel.org/r/only-walk@x\n"
    repo.object_store.add_object(extra)
    repo.refs[b"HEAD"] = extra.id
    monkeypatch.setattr(settings, "mainline_tree_path", repo_path)

    result = CliRunner().invoke(
        update_mainline_command,
        ["--skip-fetch", "--skip-maintainers"],
    )
    assert result.exit_code == 0
    assert "loaded" not in result.output  # MAINTAINERS skipped
    assert "walked 2 commits" in result.output

    from mimir.models import MainlineCommit, Subsystem

    with seeded_db() as s:
        # No subsystems loaded.
        assert s.execute(select(Subsystem)).all() == []
        # But mainline_commits got the row.
        rows = list(s.execute(select(MainlineCommit)).scalars())
    assert [r.message_id for r in rows] == ["only-walk@x"]


def test_update_mainline_errors_if_maintainers_blob_missing(
    seeded_db,
    tmp_path,
    monkeypatch,
):
    """If the tree at the configured path doesn't have a MAINTAINERS
    file at HEAD, fail loudly, almost certainly the wrong tree was
    configured. Better than silently loading zero subsystems."""
    # Build a bare repo with a different file at HEAD.
    repo_path = tmp_path / "linux.git"
    repo = Repo.init_bare(str(repo_path), mkdir=True)
    blob = Blob.from_string(b"hello")
    repo.object_store.add_object(blob)
    tree = Tree()
    tree.add(b"README", 0o100644, blob.id)
    repo.object_store.add_object(tree)
    commit = Commit()
    commit.tree = tree.id
    commit.parents = []
    commit.author = commit.committer = b"t <t@x>"
    commit.commit_time = commit.author_time = 1700000000
    commit.commit_timezone = commit.author_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = b"x"
    repo.object_store.add_object(commit)
    repo.refs[b"HEAD"] = commit.id

    monkeypatch.setattr(settings, "mainline_tree_path", repo_path)
    result = CliRunner().invoke(update_mainline_command, ["--skip-fetch"])
    assert result.exit_code != 0
    assert "no MAINTAINERS" in result.output
