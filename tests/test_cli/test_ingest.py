"""Tests for mimir/cli/ingest.py: the `ingest` command's CLI
shape + the `reindex` command (state rewind, --from-scratch
destructive mode, missing-epoch + malformed-shape
ClickException branches)."""

from click.testing import CliRunner
from sqlalchemy import select

from mimir.cli import (
    ingest_command,
    reindex_command,
)
from mimir.models import Article, ArticleList, Inbox

from tests.test_cli._helpers import _build_pubinbox_repo, _repoint_inbox, _rfc5322_msg


def test_ingest_command_runs_and_prints_per_epoch_line(seeded_db, tmp_path):
    """`flask --app mimir ingest` walks every configured inbox's mirror
    and emits one summary line per epoch. Pin both the side-effect
    (article row count) and the visible contract (output shape).

    Use epoch 2.git so the seeded fixture's epoch=0.git ArticleList
    rows don't interfere with our assertions."""
    from mimir.extensions import SessionLocal

    mirror = tmp_path / "alpha-mirror"
    _build_pubinbox_repo(
        mirror / "2.git",
        [
            _rfc5322_msg("cli-ingest-1@example.com"),
            _rfc5322_msg("cli-ingest-2@example.com"),
        ],
    )
    _repoint_inbox("alpha", mirror)

    result = CliRunner().invoke(ingest_command, ["--inbox", "alpha"])
    assert result.exit_code == 0, result.output
    # Per-epoch line shape: `<inbox>/<epoch>: new=N linked=N ...`
    assert "alpha/2.git:" in result.output
    assert "new=2" in result.output

    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        ids = (
            s.execute(
                select(Article.message_id)
                .join(ArticleList, ArticleList.article_id == Article.id)
                .where(ArticleList.inbox_id == alpha.id, ArticleList.epoch == "2.git")
            )
            .scalars()
            .all()
        )
    assert "cli-ingest-1@example.com" in ids
    assert "cli-ingest-2@example.com" in ids


def test_ingest_command_unknown_inbox_clickexception():
    """The `_select_inboxes` helper raises ClickException on an
    unknown name; the CLI must surface that as a non-zero exit."""
    result = CliRunner().invoke(ingest_command, ["--inbox", "no-such-inbox"])
    assert result.exit_code != 0
    assert "unknown inbox" in result.output


# `reindex` -- single-epoch rewind, with and without --from-scratch.


def test_reindex_default_rewinds_state_and_redrives(seeded_db, tmp_path):
    """Default reindex: doesn't delete ArticleList rows, but clears
    the IngestState cursor so the next walk starts at the beginning.
    The actual messages are dup_db skips because they're already in
    the DB; the operator-visible signal is the summary line."""
    from mimir.extensions import SessionLocal
    from mimir.models import IngestState

    mirror = tmp_path / "alpha-mirror"
    _build_pubinbox_repo(
        mirror / "2.git",
        [
            _rfc5322_msg("reindex-1@example.com"),
            _rfc5322_msg("reindex-2@example.com"),
        ],
    )
    _repoint_inbox("alpha", mirror)

    # First ingest seeds the cursor and the rows.
    CliRunner().invoke(ingest_command, ["--inbox", "alpha"])
    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        state = s.get(IngestState, (alpha.id, "2.git"))
    assert state is not None
    assert state.last_commit_sha is not None

    # Reindex without --from-scratch: cursor goes back to None, the
    # re-walk redrives the same commits, which are dup_db this time.
    result = CliRunner().invoke(reindex_command, ["alpha", "2.git"])
    assert result.exit_code == 0, result.output
    assert "alpha/2.git:" in result.output
    assert "dup_db=2" in result.output, result.output
    assert "deleted" not in result.output  # only --from-scratch logs that


def test_reindex_from_scratch_deletes_existing_links(seeded_db, tmp_path):
    """`--from-scratch` drops the per-inbox ArticleList rows for the
    epoch before re-walking, so the messages re-ingest as `linked`
    (the Article rows survive because they may be cross-posted)."""
    from sqlalchemy import func
    from mimir.extensions import SessionLocal

    mirror = tmp_path / "alpha-mirror"
    _build_pubinbox_repo(
        mirror / "2.git",
        [
            _rfc5322_msg("scratch-1@example.com"),
            _rfc5322_msg("scratch-2@example.com"),
        ],
    )
    _repoint_inbox("alpha", mirror)
    CliRunner().invoke(ingest_command, ["--inbox", "alpha"])

    with SessionLocal() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        before = s.execute(
            select(func.count())
            .select_from(ArticleList)
            .where(ArticleList.inbox_id == alpha.id, ArticleList.epoch == "2.git")
        ).scalar_one()
    assert before == 2

    result = CliRunner().invoke(reindex_command, ["alpha", "2.git", "--from-scratch"])
    assert result.exit_code == 0, result.output
    assert "deleted 2 existing inbox-links for alpha/2.git" in result.output
    # The re-walk re-adds them via the linked-bucket path.
    assert "linked=2" in result.output, result.output


def test_reindex_missing_epoch_repo_clickexception(seeded_db, tmp_path):
    """Reindex against an epoch directory that doesn't exist on disk
    must raise a clean ClickException, not crash inside dulwich."""
    _repoint_inbox("alpha", tmp_path / "alpha-mirror")  # dir doesn't exist
    result = CliRunner().invoke(reindex_command, ["alpha", "0.git"])
    assert result.exit_code != 0
    assert "epoch repo not found" in result.output


def test_reindex_rejects_malformed_epoch_shape(seeded_db, tmp_path):
    """The epoch argument is joined onto inbox.mirror_path. Without
    a shape check, an operator typo or hostile input like
    `../../etc` would walk outside the mirror root before the
    `.exists()` guard ever runs. The regex pins the public-inbox
    convention (`<N>.git`) at the CLI boundary."""
    _repoint_inbox("alpha", tmp_path / "alpha-mirror")
    for bad in ("..", "../../etc", "/etc", "0.gitX", "0", "abc", "0.git/.."):
        result = CliRunner().invoke(reindex_command, ["alpha", bad])
        assert result.exit_code != 0, (
            f"epoch {bad!r} should be rejected, got exit_code=0\n"
            f"output: {result.output}"
        )
        assert "epoch" in result.output.lower()
