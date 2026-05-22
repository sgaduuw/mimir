"""Tests for mimir/cli/admin/inbox.py: the `admin inbox`
subcommand group (list, show, add, update, remove,
trackers {show,set,add,remove,clear}) including the
ClickException branches on unknown inboxes / invalid args /
malformed inputs and the remove --yes / --keep-orphans /
missing-mirror branches."""

from click.testing import CliRunner
from sqlalchemy import select

from mimir.cli import (
    admin_inbox_add_command,
    admin_inbox_list_command,
    admin_inbox_remove_command,
    admin_inbox_show_command,
    admin_inbox_trackers_add_command,
    admin_inbox_trackers_clear_command,
    admin_inbox_trackers_remove_command,
    admin_inbox_trackers_set_command,
    admin_inbox_trackers_show_command,
    admin_inbox_update_command,
)
from mimir.inboxes import get_inbox, set_tracked_authors
from mimir.models import Article, Inbox


def test_trackers_show_no_trackers(seeded_db):
    result = CliRunner().invoke(admin_inbox_trackers_show_command, ["alpha"])
    assert result.exit_code == 0
    assert "no trackers configured" in result.output


def test_trackers_show_with_trackers(seeded_db):
    set_tracked_authors("alpha", {"Linus": "torvalds@", "Greg": "gregkh@"})
    result = CliRunner().invoke(admin_inbox_trackers_show_command, ["alpha"])
    assert result.exit_code == 0
    assert "2 tracker(s)" in result.output
    assert "Linus" in result.output
    assert "torvalds@" in result.output
    assert "Greg" in result.output


def test_trackers_show_unknown_inbox(seeded_db):
    result = CliRunner().invoke(admin_inbox_trackers_show_command, ["nope"])
    assert result.exit_code != 0
    assert "no inbox" in result.output


def test_trackers_set_replaces_dict(seeded_db):
    set_tracked_authors("alpha", {"old": "stale@"})
    result = CliRunner().invoke(
        admin_inbox_trackers_set_command,
        ["alpha", "Linus=torvalds@", "Greg=gregkh@"],
    )
    assert result.exit_code == 0
    assert get_inbox("alpha").tracked_authors == {
        "Linus": "torvalds@",
        "Greg": "gregkh@",
    }


def test_trackers_set_rejects_malformed_pair(seeded_db):
    result = CliRunner().invoke(
        admin_inbox_trackers_set_command,
        ["alpha", "missing-equals"],
    )
    assert result.exit_code != 0
    assert "LABEL=SUBSTRING" in result.output


def test_trackers_set_keeps_value_with_embedded_equals(seeded_db):
    """A value containing `=` should survive the split-on-first-`=`."""
    result = CliRunner().invoke(
        admin_inbox_trackers_set_command,
        ["alpha", "Weird=foo=bar@"],
    )
    assert result.exit_code == 0
    assert get_inbox("alpha").tracked_authors == {"Weird": "foo=bar@"}


def test_trackers_add_initializes_null(seeded_db):
    result = CliRunner().invoke(
        admin_inbox_trackers_add_command,
        ["alpha", "Linus", "torvalds@"],
    )
    assert result.exit_code == 0
    assert get_inbox("alpha").tracked_authors == {"Linus": "torvalds@"}


def test_trackers_add_appends(seeded_db):
    set_tracked_authors("alpha", {"Linus": "torvalds@"})
    result = CliRunner().invoke(
        admin_inbox_trackers_add_command,
        ["alpha", "Greg", "gregkh@"],
    )
    assert result.exit_code == 0
    assert get_inbox("alpha").tracked_authors == {
        "Linus": "torvalds@",
        "Greg": "gregkh@",
    }


def test_trackers_remove_drops_label(seeded_db):
    set_tracked_authors("alpha", {"Linus": "torvalds@", "Greg": "gregkh@"})
    result = CliRunner().invoke(
        admin_inbox_trackers_remove_command,
        ["alpha", "Greg"],
    )
    assert result.exit_code == 0
    assert get_inbox("alpha").tracked_authors == {"Linus": "torvalds@"}


def test_trackers_remove_missing_label_fails(seeded_db):
    set_tracked_authors("alpha", {"Linus": "torvalds@"})
    result = CliRunner().invoke(
        admin_inbox_trackers_remove_command,
        ["alpha", "Greg"],
    )
    assert result.exit_code != 0
    assert "no tracker labelled" in result.output


def test_trackers_clear_writes_null(seeded_db):
    set_tracked_authors("alpha", {"Linus": "torvalds@"})
    result = CliRunner().invoke(
        admin_inbox_trackers_clear_command,
        ["alpha"],
    )
    assert result.exit_code == 0
    assert get_inbox("alpha").tracked_authors is None


def test_admin_inbox_list_shows_tracker_count(seeded_db):
    set_tracked_authors("alpha", {"Linus": "torvalds@", "Greg": "gregkh@"})
    result = CliRunner().invoke(admin_inbox_list_command, [])
    assert result.exit_code == 0
    # alpha has two trackers, beta has none.
    lines = result.output.strip().splitlines()
    alpha_line = next(line for line in lines if " alpha " in line)
    beta_line = next(line for line in lines if " beta " in line)
    assert "trackers=2" in alpha_line
    assert "trackers=none" in beta_line


def test_admin_inbox_add_creates_inbox(seeded_db):
    result = CliRunner().invoke(
        admin_inbox_add_command,
        [
            "gamma",
            "--mirror-path",
            "/tmp/gamma",
            "--upstream-url",
            "https://example.com/gamma",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "created inbox 'gamma'" in result.output
    # Confirm it landed in the DB.
    assert get_inbox("gamma").upstream_url == "https://example.com/gamma"


def test_admin_inbox_add_defaults_to_lore_layout(seeded_db):
    """With only NAME, mirror_path and upstream_url default to the
    conventional lore.kernel.org public-inbox shape."""
    result = CliRunner().invoke(admin_inbox_add_command, ["linux-arm-kernel"])
    assert result.exit_code == 0, result.output
    inbox = get_inbox("linux-arm-kernel")
    assert inbox.mirror_path == "Inboxes/linux-arm-kernel/git"
    assert inbox.upstream_url == "https://lore.kernel.org/linux-arm-kernel"
    # Resolved values are echoed so the operator sees what got stored.
    assert "Inboxes/linux-arm-kernel/git" in result.output
    assert "https://lore.kernel.org/linux-arm-kernel" in result.output


def test_admin_inbox_add_partial_override(seeded_db):
    """Either flag can override independently; the other still
    falls back to the default."""
    result = CliRunner().invoke(
        admin_inbox_add_command,
        [
            "linux-fsdevel-mirror",
            "--mirror-path",
            "/srv/custom/fsdevel/git",
        ],
    )
    assert result.exit_code == 0, result.output
    inbox = get_inbox("linux-fsdevel-mirror")
    assert inbox.mirror_path == "/srv/custom/fsdevel/git"
    assert inbox.upstream_url == "https://lore.kernel.org/linux-fsdevel-mirror"


def test_admin_inbox_add_invalid_url_clickexception(seeded_db):
    """`upstream_url` must be `https://...`; the validator raises
    InboxValidationError, which the CLI surfaces as a ClickException."""
    result = CliRunner().invoke(
        admin_inbox_add_command,
        [
            "gamma",
            "--mirror-path",
            "/tmp/gamma",
            "--upstream-url",
            "not-a-url",
        ],
    )
    assert result.exit_code != 0


def test_admin_inbox_update_no_args_clickexception(seeded_db):
    """Update must specify at least one field to change; calling with
    none is a user error."""
    result = CliRunner().invoke(admin_inbox_update_command, ["alpha"])
    assert result.exit_code != 0
    assert "nothing to update" in result.output


def test_admin_inbox_update_changes_mirror_path(seeded_db):
    result = CliRunner().invoke(
        admin_inbox_update_command,
        [
            "alpha",
            "--mirror-path",
            "/tmp/alpha-new",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "updated inbox 'alpha'" in result.output
    assert get_inbox("alpha").mirror_path == "/tmp/alpha-new"


def test_admin_inbox_show_prints_fields_and_states(seeded_db):
    """`admin inbox show <name>` prints the configured fields plus
    the per-epoch ingest cursor state. The seeded alpha has linked
    articles but no IngestState row -- exercise both branches in
    one test by checking the "none -- never ingested" line."""
    result = CliRunner().invoke(admin_inbox_show_command, ["alpha"])
    assert result.exit_code == 0, result.output
    assert "name:" in result.output and "alpha" in result.output
    assert "mirror_path:" in result.output
    assert "upstream_url:" in result.output
    # Seeded fixture has 3 ArticleList rows on alpha (art1, art3, art4).
    assert "linked articles: 3" in result.output
    # No IngestState rows yet.
    assert "never ingested" in result.output


def test_admin_inbox_show_unknown_clickexception(seeded_db):
    result = CliRunner().invoke(admin_inbox_show_command, ["no-such-inbox"])
    assert result.exit_code != 0


def test_admin_inbox_remove_yes_drops_inbox_and_orphans(seeded_db):
    """With `--yes`, the prompt is skipped; the inbox row + its
    ArticleList rows go away. Orphan articles (those left with no
    remaining inbox links) also go by default."""
    from mimir.extensions import SessionLocal

    # Pre-state: alpha exists, art1+art4 are alpha-only.
    with SessionLocal() as s:
        assert (
            s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one_or_none()
            is not None
        )
        ids_before = set(s.execute(select(Article.message_id)).scalars().all())
    assert {"art1@example.com", "art4@example.com"} <= ids_before

    result = CliRunner().invoke(admin_inbox_remove_command, ["alpha", "--yes"])
    assert result.exit_code == 0, result.output
    assert "removed inbox 'alpha'" in result.output
    assert "article_lists rows deleted:" in result.output

    with SessionLocal() as s:
        assert (
            s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one_or_none()
            is None
        )
        ids_after = set(s.execute(select(Article.message_id)).scalars().all())
    # art1 and art4 were alpha-only -> gone. art3 was cross-posted
    # with beta -> survives. art2 is beta-only -> survives.
    assert "art1@example.com" not in ids_after
    assert "art4@example.com" not in ids_after
    assert "art2@example.com" in ids_after
    assert "art3@example.com" in ids_after


def test_admin_inbox_update_unknown_clickexception(seeded_db):
    """update on a non-existent inbox surfaces InboxNotFound as a
    ClickException (covers cli.py:1383-1384)."""
    result = CliRunner().invoke(
        admin_inbox_update_command,
        [
            "no-such-inbox",
            "--mirror-path",
            "/tmp/anywhere",
        ],
    )
    assert result.exit_code != 0
    assert "no-such-inbox" in result.output


def test_admin_inbox_update_invalid_value_clickexception(seeded_db):
    """update with an invalid upstream_url surfaces InboxValidationError
    as a ClickException (covers cli.py:1385-1386)."""
    result = CliRunner().invoke(
        admin_inbox_update_command,
        [
            "alpha",
            "--upstream-url",
            "not-a-url",
        ],
    )
    assert result.exit_code != 0


def test_admin_inbox_remove_unknown_clickexception(seeded_db):
    """remove on a non-existent inbox raises ClickException via the
    pre-flight get_inbox call (covers cli.py:1422-1423)."""
    result = CliRunner().invoke(
        admin_inbox_remove_command,
        [
            "no-such-inbox",
            "--yes",
        ],
    )
    assert result.exit_code != 0
    assert "no-such-inbox" in result.output


def test_admin_inbox_remove_inbox_data_skips_when_mirror_absent(
    seeded_db,
    tmp_path,
):
    """--remove-inbox-data announces the rm target but skips the
    confirm prompt when the path doesn't exist on disk (covers
    cli.py:1426-1428). The seeded `alpha` inbox uses `/tmp/alpha`
    which is absent in CI."""
    # Point alpha at a path that definitely doesn't exist.
    absent = tmp_path / "definitely-not-a-mirror"
    result = CliRunner().invoke(
        admin_inbox_update_command,
        [
            "alpha",
            "--mirror-path",
            str(absent),
        ],
    )
    assert result.exit_code == 0, result.output

    result = CliRunner().invoke(
        admin_inbox_remove_command,
        [
            "alpha",
            "--yes",
            "--remove-inbox-data",
        ],
    )
    assert result.exit_code == 0, result.output
    # Announcement happens regardless of existence.
    assert "--remove-inbox-data set" in result.output
    # No "removed on-disk mirror" line because the path was absent.
    assert "removed on-disk mirror" not in result.output


def test_admin_inbox_remove_keep_orphans_preserves_articles(seeded_db):
    """`--keep-orphan-articles` leaves Article rows with no remaining
    links intact (article_lists rows go either way)."""
    from mimir.extensions import SessionLocal

    result = CliRunner().invoke(
        admin_inbox_remove_command,
        [
            "alpha",
            "--yes",
            "--keep-orphan-articles",
        ],
    )
    assert result.exit_code == 0, result.output
    # Orphan-deleted line is suppressed under --keep-orphan-articles.
    assert "orphan articles deleted" not in result.output

    with SessionLocal() as s:
        ids = set(s.execute(select(Article.message_id)).scalars().all())
    # All four seed articles survive: alpha-only ones are now orphans
    # but still present.
    assert "art1@example.com" in ids
    assert "art4@example.com" in ids
    assert "art2@example.com" in ids
    assert "art3@example.com" in ids


# Structural / wire-up assertions.
#
# The shape of `register_cli` is load-bearing, Flask only exposes the
# subcommands explicitly added there. A new top-level `@click.command`
# decorator at module scope that doesn't get wired in is invisible to
# the operator and to CI (every CLI test invokes its target directly,
# not via `flask --app mimir <name>`), so the regression slips through.
