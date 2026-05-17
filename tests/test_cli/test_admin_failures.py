"""Tests for mimir/cli/admin/failures.py: `admin failures`
(list with --epoch filter, replay happy path + still-failing
+ unknown-inbox + epoch-isolation)."""



from click.testing import CliRunner
from dulwich.repo import Repo
from sqlalchemy import select

from mimir.cli import (
    admin_failures_list_command,
    admin_failures_replay_command,
)
from mimir.models import Inbox, ParseFailure

from tests.test_cli._helpers import _build_pubinbox_repo, _repoint_inbox, _rfc5322_msg, _seed_parse_failure


def test_admin_failures_list_empty_says_so(seeded_db):
    result = CliRunner().invoke(admin_failures_list_command, [])
    assert result.exit_code == 0
    assert "no parse failures" in result.output


def test_admin_failures_list_prints_seeded_row_and_total(seeded_db):
    _seed_parse_failure()
    result = CliRunner().invoke(admin_failures_list_command, [])
    assert result.exit_code == 0, result.output
    assert "alpha/0.git@" in result.output  # `<inbox>/<epoch>@<sha[:12]>`
    assert "MessageTooLarge" in result.output
    assert "message exceeds cap" in result.output
    assert "total: 1" in result.output


def test_admin_failures_list_epoch_requires_inbox(seeded_db):
    """`--epoch` without `--inbox` is rejected up-front; the filter
    only makes sense scoped to an inbox."""
    result = CliRunner().invoke(admin_failures_list_command, ["--epoch", "0.git"])
    assert result.exit_code != 0
    assert "--epoch requires --inbox" in result.output


def test_admin_failures_replay_unknown_inbox_clickexception(seeded_db):
    result = CliRunner().invoke(admin_failures_replay_command, ["no-such-inbox"])
    assert result.exit_code != 0
    # InboxNotFound is the underlying type; the CLI surfaces its str().
    assert "no-such-inbox" in result.output


def test_admin_failures_replay_happy_path_recovers_and_clears(
    seeded_db, tmp_path, monkeypatch,
):
    """The replay happy path: stage a failure for a real parseable
    blob, invoke the CLI, assert the summary line reports
    `recovered=1` and the row is gone. Only `unknown_inbox` was
    covered before; the success branch, the one operators actually
    invoke after a parser fix, was unreached.

    Mirror the test_ingest replay setup but drive it through the
    Click runner so the CLI argument parsing + ClickException
    boundary + output-shape are all exercised end-to-end."""
    import datetime as _dt
    import mimir.parser
    from sqlalchemy import func
    from mimir.extensions import SessionLocal

    # Build a real repo so replay_failures can fetch the blob.
    mirror_root = tmp_path / "replay-cli-mirror"
    mirror_root.mkdir()
    _build_pubinbox_repo(mirror_root / "0.git", [
        _rfc5322_msg("cli-replay@example.com", body=b"x" * 500),
    ])
    _repoint_inbox("alpha", mirror_root)

    # Stage a failure for the real commit_sha so replay finds work.
    repo = Repo(str(mirror_root / "0.git"))
    sha = repo.head().decode()
    now = _dt.datetime.now(_dt.timezone.utc)
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.add(ParseFailure(
            inbox_id=ix.id,
            epoch="0.git",
            commit_sha=sha,
            error_class="MessageTooLarge",
            error_message="prior cap was too tight",
            first_seen=now,
            last_attempt=now,
            attempts=1,
        ))
        s.commit()

    # Ensure parser MAX_RAW_MESSAGE_BYTES doesn't reject the blob.
    monkeypatch.setattr(mimir.parser, "MAX_RAW_MESSAGE_BYTES", 50_000_000)

    result = CliRunner().invoke(admin_failures_replay_command, ["alpha"])
    assert result.exit_code == 0, result.output
    assert "alpha: attempted=1 recovered=1 still_failed=0 skipped=0" in result.output

    # The failure row is gone.
    with SessionLocal() as s:
        remaining = s.execute(
            select(func.count()).select_from(ParseFailure)
            .where(ParseFailure.commit_sha == sha)
        ).scalar_one()
    assert remaining == 0


def test_admin_failures_replay_epoch_filter_isolates_one_epoch(seeded_db):
    """`--epoch N.git` restricts the replay to that epoch's failure
    rows; rows in other epochs must not be `attempted` (covers
    cli.py + ingest.py:511-512 selective-replay branch).

    Operators reach for `--epoch` when a parser fix only affects one
    epoch's corpus shape (e.g. a multipart variant only present in
    the lkml 2020-era epoch), wanting to keep newer epochs out of
    scope. A regression that ignored the filter would silently
    replay everything."""
    import datetime as _dt
    from mimir.extensions import SessionLocal

    now = _dt.datetime.now(_dt.timezone.utc)
    with SessionLocal() as s:
        ix = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        s.add_all([
            ParseFailure(
                inbox_id=ix.id, epoch="0.git", commit_sha="aa" * 20,
                error_class="X", error_message="x", first_seen=now,
                last_attempt=now, attempts=1,
            ),
            ParseFailure(
                inbox_id=ix.id, epoch="1.git", commit_sha="bb" * 20,
                error_class="X", error_message="x", first_seen=now,
                last_attempt=now, attempts=1,
            ),
        ])
        s.commit()

    result = CliRunner().invoke(
        admin_failures_replay_command, ["alpha", "--epoch", "0.git"],
    )
    assert result.exit_code == 0, result.output
    # Mirror is absent, so 0.git's row gets `skipped`, not `recovered`.
    # The filter assertion is on `attempted=1`: only the 0.git row
    # entered the loop. Without the filter `attempted` would be 2.
    assert "attempted=1" in result.output
    assert "skipped=1" in result.output

    # Both rows remain in the DB: 0.git was skipped (mirror absent,
    # continue without delete) and 1.git was filtered out entirely.
    # The filter assertion is the `attempted=1` count above; this
    # secondary check pins that no row was silently consumed.
    with SessionLocal() as s:
        epochs = set(s.execute(
            select(ParseFailure.epoch).select_from(ParseFailure)
        ).scalars().all())
    assert epochs == {"0.git", "1.git"}


# `init-db` -- bootstrap helper; documented in CLAUDE.md as a quick
# local-dev path that the operator falls back to when alembic isn't
# applicable. Untested historically because alembic is the real
# migration story.
