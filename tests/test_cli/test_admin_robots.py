"""Tests for mimir/cli/admin/robots.py: the `admin robots` subcommand
group (list, show, add, update, remove, reset).

The CLI surface was unmaintained-by-test-coverage before the
2026-06-02 audit (audit notes "Admin robots CRUD not tested" — `tests/`
search confirmed). `mimir/robots.py` service-layer tests live in
`tests/test_robots.py` and cover validators / `add_rule` / `reset_rules`,
but the CLI shim that operators actually use was silent.

Routes the writes through the session broker via `get_broker_client()`
(per `mimir/cli/admin/robots.py:160-170`), so the conftest's
session-scoped broker fixture is what makes these tests work.
"""

from click.testing import CliRunner
from sqlalchemy import select

from mimir.cli.admin.robots import (
    admin_robots_add_command,
    admin_robots_list_command,
    admin_robots_remove_command,
    admin_robots_reset_command,
    admin_robots_show_command,
    admin_robots_update_command,
)
from mimir.models import RobotsRule


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_admin_robots_list_renders_seeded_star_rule(seeded_db):
    """The conftest re-seeds the migration `*` row via
    `mimir.robots.reset_rules()` before every test. `list` must
    surface it; the format includes the user-agent on the leading
    column."""
    result = CliRunner().invoke(admin_robots_list_command)
    assert result.exit_code == 0, result.output
    # The seeded `*` row appears in `list` output (with whitespace
    # padding to align columns).
    assert "*" in result.output
    # Other tokens from the row's representation surface too:
    # crawl_delay (5 per migration), disallow paths (the attachment
    # exclusion path is the canonical seed entry).
    assert "crawl_delay" in result.output
    assert "disallow" in result.output


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_admin_robots_show_for_seeded_star_rule(seeded_db):
    result = CliRunner().invoke(admin_robots_show_command, ["*"])
    assert result.exit_code == 0, result.output
    assert "user_agent:" in result.output
    assert "*" in result.output


def test_admin_robots_show_unknown_user_agent_clickexception(seeded_db):
    result = CliRunner().invoke(admin_robots_show_command, ["NoSuchBot"])
    assert result.exit_code != 0
    assert "no rule" in result.output.lower()
    assert "NoSuchBot" in result.output


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_admin_robots_add_persists_new_row(seeded_db):
    """`admin robots add` lands a new `robots_rules` row through the
    broker, with the user_agent + disallow paths + crawl_delay the
    CLI surfaced. Persistence is the load-bearing contract: the
    `/robots.txt` route renders directly from the table on every
    request, so the row IS the visible robots policy."""
    result = CliRunner().invoke(
        admin_robots_add_command,
        ["GPTBot", "--disallow", "/", "--crawl-delay", "30"],
    )
    assert result.exit_code == 0, result.output
    assert "GPTBot" in result.output

    with seeded_db() as s:
        row = s.execute(
            select(RobotsRule).where(RobotsRule.user_agent == "GPTBot")
        ).scalar_one_or_none()
    assert row is not None, (
        "robots_rules row must exist after `admin robots add`; the route "
        "renders from this table on every request, so a silent persistence "
        "drop would mean the policy never reaches crawlers"
    )
    assert row.crawl_delay == 30
    assert row.disallow_paths == ["/"]


def test_admin_robots_add_with_content_signal_persists(seeded_db):
    """The content-signal pairs are validated then upserted; ensure
    the round-trip surfaces them on the persisted row."""
    result = CliRunner().invoke(
        admin_robots_add_command,
        [
            "Googlebot",
            "--content-signal",
            "search=yes",
            "--content-signal",
            "ai-train=no",
        ],
    )
    assert result.exit_code == 0, result.output

    with seeded_db() as s:
        row = s.execute(
            select(RobotsRule).where(RobotsRule.user_agent == "Googlebot")
        ).scalar_one()
    assert row.content_signals == {"search": "yes", "ai-train": "no"}


def test_admin_robots_add_duplicate_user_agent_clickexception(seeded_db):
    """A second `add` for the same UA must error rather than silently
    duplicate. The DB enforces this via the UNIQUE constraint on
    `robots_rules.user_agent`; the broker handler surfaces the
    violation as a ClickException."""
    runner = CliRunner()
    first = runner.invoke(admin_robots_add_command, ["DupeBot", "--disallow", "/"])
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        admin_robots_add_command, ["DupeBot", "--disallow", "/private/"]
    )
    assert second.exit_code != 0, (
        "second `add` for the same user_agent must error; the UNIQUE "
        "constraint on robots_rules.user_agent must not be silently "
        "swallowed"
    )


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_admin_robots_update_adds_disallow_path(seeded_db):
    """Operator can append a new Disallow path via
    `update --add-disallow`. The list mutates in place; the rest of
    the row is untouched."""
    runner = CliRunner()
    runner.invoke(admin_robots_add_command, ["UpBot", "--disallow", "/old/"])
    result = runner.invoke(
        admin_robots_update_command, ["UpBot", "--add-disallow", "/new/"]
    )
    assert result.exit_code == 0, result.output

    with seeded_db() as s:
        row = s.execute(
            select(RobotsRule).where(RobotsRule.user_agent == "UpBot")
        ).scalar_one()
    assert set(row.disallow_paths) == {"/old/", "/new/"}


def test_admin_robots_update_unknown_user_agent_clickexception(seeded_db):
    """`update` on a UA that has no row must error, not silently
    create one. Symmetric to `inbox_update`'s no-such-inbox path."""
    result = CliRunner().invoke(
        admin_robots_update_command, ["NoSuchBot", "--add-disallow", "/"]
    )
    assert result.exit_code != 0
    assert "no rule" in result.output.lower() or "NoSuchBot" in result.output


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_admin_robots_remove_drops_the_row(seeded_db):
    """`remove` deletes the row from `robots_rules`; the route's
    rendered `/robots.txt` no longer includes that user-agent stanza
    on subsequent requests (the route reads on every request)."""
    runner = CliRunner()
    runner.invoke(admin_robots_add_command, ["DeleteMe", "--disallow", "/"])

    with seeded_db() as s:
        assert (
            s.execute(
                select(RobotsRule).where(RobotsRule.user_agent == "DeleteMe")
            ).scalar_one_or_none()
            is not None
        )

    result = runner.invoke(admin_robots_remove_command, ["DeleteMe"])
    assert result.exit_code == 0, result.output

    with seeded_db() as s:
        assert (
            s.execute(
                select(RobotsRule).where(RobotsRule.user_agent == "DeleteMe")
            ).scalar_one_or_none()
            is None
        )


def test_admin_robots_remove_refuses_star_user_agent(seeded_db):
    """The `*` stanza is structural (migration-seeded). `remove *`
    must refuse so the operator doesn't accidentally leave the
    archive with no default rule. `reset` is the right tool to
    restore the seeded `*` defaults."""
    result = CliRunner().invoke(admin_robots_remove_command, ["*"])
    assert result.exit_code != 0, (
        "remove must refuse the structural `*` stanza (operator should "
        "use `reset` instead); silently allowing removal would leave the "
        "archive with no default robots rule"
    )

    with seeded_db() as s:
        # `*` row survives the failed `remove`.
        assert (
            s.execute(
                select(RobotsRule).where(RobotsRule.user_agent == "*")
            ).scalar_one_or_none()
            is not None
        )


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def test_admin_robots_reset_drops_added_rows_and_reseeds_star(seeded_db):
    """`reset --yes` drops every `robots_rules` row and re-seeds the
    `*` stanza with the migration defaults. Operator-facing "undo"
    for any accidental admin-robots edit session."""
    runner = CliRunner()
    runner.invoke(admin_robots_add_command, ["TempBot1", "--disallow", "/"])
    runner.invoke(admin_robots_add_command, ["TempBot2", "--disallow", "/x/"])

    result = runner.invoke(admin_robots_reset_command, ["--yes"])
    assert result.exit_code == 0, result.output

    with seeded_db() as s:
        all_uas = {row[0] for row in s.execute(select(RobotsRule.user_agent)).all()}
    # Reset drops the manually-added rows.
    assert "TempBot1" not in all_uas
    assert "TempBot2" not in all_uas
    # And re-seeds the structural `*` row.
    assert "*" in all_uas, (
        "reset must re-seed the `*` row from the migration defaults; "
        "leaving the archive with no `*` rule would silently strip the "
        "default Disallow + Crawl-delay from /robots.txt"
    )


def test_admin_robots_reset_without_yes_requires_confirmation(seeded_db):
    """Without `--yes`, `reset` prompts for confirmation. CliRunner
    treats an unanswered prompt as input EOF, which Click reports
    as an `Aborted!` error. Catches a refactor that drops the
    confirmation prompt (which is the only thing standing between
    an operator typo and dropping every custom rule)."""
    # Default CliRunner input is "", which Click reads as EOF on a
    # confirm() — abort.
    result = CliRunner().invoke(admin_robots_reset_command)
    assert result.exit_code != 0, (
        "reset without --yes must prompt for confirmation; a regression "
        "that dropped the confirm() call would silently drop every row "
        "on operator typo"
    )
