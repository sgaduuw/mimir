"""Tests for mimir/cli/show.py: `show <msgid>` printing the DB
row + parsed blob, no-body suppression, body-char truncation
marker, unknown-msgid + inbox-filter ClickException
branches."""

from click.testing import CliRunner

from mimir.cli import (
    ingest_command,
    show_command,
)

from tests.test_cli._helpers import (
    _build_pubinbox_repo,
    _ingest_one_for_show,
    _repoint_inbox,
    _rfc5322_msg,
)


def test_show_prints_db_row_and_parsed_blob(seeded_db, tmp_path):
    msgid, _ = _ingest_one_for_show(tmp_path)
    result = CliRunner().invoke(show_command, [msgid])
    assert result.exit_code == 0, result.output
    # DB-side section
    assert "--- DB row ---" in result.output
    assert "linked inboxes:" in result.output
    assert "alpha/2.git@" in result.output
    # Parsed-blob section
    assert "--- parsed blob ---" in result.output
    assert f"Message-ID: {msgid}" in result.output
    assert "show test subject" in result.output
    assert "hello show body" in result.output


def test_show_no_body_suppresses_body(seeded_db, tmp_path):
    msgid, _ = _ingest_one_for_show(tmp_path)
    result = CliRunner().invoke(show_command, [msgid, "--no-body"])
    assert result.exit_code == 0, result.output
    assert "show test subject" in result.output  # headers still shown
    assert "hello show body" not in result.output


def test_show_body_chars_truncates_with_marker(seeded_db, tmp_path):
    """When `--body-chars` is shorter than the body, the output is
    truncated and a `... (N more chars truncated; ...)` line
    explains it."""
    long_body = b"x" * 200
    msgid = "show-trunc@example.com"
    mirror = tmp_path / "alpha-mirror"
    _build_pubinbox_repo(
        mirror / "2.git",
        [
            _rfc5322_msg(msgid, body=long_body),
        ],
    )
    _repoint_inbox("alpha", mirror)
    CliRunner().invoke(ingest_command, ["--inbox", "alpha"])

    result = CliRunner().invoke(show_command, [msgid, "--body-chars", "50"])
    assert result.exit_code == 0, result.output
    assert "more chars truncated" in result.output
    # 150 chars were dropped; the marker spells it out.
    assert "150" in result.output


def test_show_unknown_message_id_clickexception(seeded_db):
    result = CliRunner().invoke(show_command, ["nope@nowhere.invalid"])
    assert result.exit_code != 0
    assert "no article" in result.output


def test_show_inbox_filter_rejects_unlinked(seeded_db, tmp_path):
    """`--inbox <name>` restricts the blob read to that inbox; if the
    article isn't linked there, a ClickException lists where it
    actually lives instead of silently falling back."""
    msgid, _ = _ingest_one_for_show(tmp_path)
    result = CliRunner().invoke(show_command, [msgid, "--inbox", "beta"])
    assert result.exit_code != 0
    assert "not linked to inbox 'beta'" in result.output
    # The error includes the list of inboxes that DO have it.
    assert "alpha" in result.output


# `vacuum` / `analyze` -- DB-maintenance commands.
