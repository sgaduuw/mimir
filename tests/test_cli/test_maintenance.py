"""Tests for mimir/cli/maintenance.py: the `update` command's
default-silent vs verbose-on-`-v` output shape (the
scheduler-verbosity rework)."""



from click.testing import CliRunner

from mimir.cli import (
    update_command,
)


def test_update_default_silent_on_no_op(seeded_db, monkeypatch):
    """No-op ticks (no upstream changes, no new commits to ingest)
    must not emit per-inbox / per-epoch lines at default verbosity
    that's what makes the scheduler log readable as inbox count grows."""
    from mimir import sync as sync_mod
    from mimir.cli import ingest as cli
    from mimir.ingest import IngestResult

    def _fake_sync(*_a, **_kw):
        return sync_mod.SyncResult(cloned=[], fetched=[], failed=[])
    def _fake_ingest_all(inboxes, limit=None, workers=1):
        return {
            name: [IngestResult(
                epoch="0.git", new=0, linked=0, dup_batch=3,
                dup_db=2, failed=0, last_commit_sha="aa" * 20,
            )]
            for name in inboxes
        }
    monkeypatch.setattr(cli, "sync_epochs", _fake_sync)
    monkeypatch.setattr(cli, "ingest_all", _fake_ingest_all)

    result = CliRunner().invoke(update_command, [])
    assert result.exit_code == 0
    assert "sync:" not in result.output, result.output
    assert "/0.git:" not in result.output, result.output


def test_update_verbose_prints_no_op_lines(seeded_db, monkeypatch):
    """-v restores per-inbox / per-epoch lines even when nothing changed."""
    from mimir import sync as sync_mod
    from mimir.cli import ingest as cli
    from mimir.ingest import IngestResult

    monkeypatch.setattr(
        cli, "sync_epochs",
        lambda *_a, **_kw: sync_mod.SyncResult(cloned=[], fetched=[], failed=[]),
    )
    monkeypatch.setattr(
        cli, "ingest_all",
        lambda inboxes, limit=None, workers=1: {
            name: [IngestResult(
                epoch="0.git", new=0, linked=0, dup_batch=1,
                dup_db=1, failed=0, last_commit_sha="bb" * 20,
            )]
            for name in inboxes
        },
    )

    result = CliRunner().invoke(update_command, ["-v"])
    assert result.exit_code == 0
    assert "sync: cloned=[] fetched=[] failed=[]" in result.output
    assert "/0.git: new=0 linked=0" in result.output


# `dev-seed-thread` builds a synthetic multi-message thread into a bare
# git repo under <mirror-root>/<inbox>/git/, then ingests it. The CLI is
# dev-only but the contract still needs pinning so a refactor of the
# synth-thread shape (depth, in_reply_to chain, author dedup) doesn't
# silently produce something that looks fine via `flask run` but breaks
# real ingest paths the next time it's exercised.
