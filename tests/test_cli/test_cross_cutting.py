"""Tests for the mimir/cli/__init__.py surface: register_cli
attaches every module-level command, the `mimir` entry-
point help lists registered commands, and
`configure_logging` is idempotent across re-invocations
(works around `logging.basicConfig`'s first-call-only
semantics)."""



from click.testing import CliRunner



def test_configure_logging_actually_changes_level_on_re_invocation():
    """`logging.basicConfig` is a no-op after the first call. The CLI
    used to call it on every invocation, so a quiet first run pinned
    the level for the whole process and subsequent `-vv` runs silently
    kept WARNING. Pin the level-flip contract: two calls with
    different verbose values must produce different effective levels.
    Audit (2026-05-15)."""
    import logging

    from mimir.cli import _configure_logging

    root = logging.getLogger()
    original_level = root.level
    try:
        _configure_logging(0)  # WARNING
        first = root.level
        _configure_logging(2)  # DEBUG
        second = root.level

        assert first == logging.WARNING
        assert second == logging.DEBUG
        assert first != second, (
            "second _configure_logging call didn't change the root "
            "level -- basicConfig idempotency regressed"
        )
    finally:
        root.setLevel(original_level)


def test_register_cli_attaches_every_module_level_command():
    """Every `@click.command`/`@click.group`-decorated callable at
    `mimir.cli` module scope must be reachable through `app.cli`
    after `register_cli(app)` runs, either added directly to
    `app.cli` or attached to a registered group (`admin`,
    `admin inbox`, `admin inbox trackers`, `admin failures`,
    `admin canonicals`).

    A regression here is silent: the command still imports clean,
    every direct-invocation test in this file still passes, but
    `flask --app mimir <name>` returns "No such command." Pin the
    surface by traversing the registered command tree and asserting
    no module-level Command falls outside it."""
    import click
    from flask import Flask

    import mimir.cli as cli_mod

    app = Flask(__name__)
    cli_mod.register_cli(app)

    def reachable(commands: dict) -> set:
        out = set()
        for cmd in commands.values():
            out.add(cmd)
            if isinstance(cmd, click.Group):
                out.update(reachable(cmd.commands))
        return out

    attached = reachable(app.cli.commands)

    declared = {
        v for v in vars(cli_mod).values() if isinstance(v, click.Command)
    }
    # `mimir` (the FlaskGroup-based standalone entry point introduced
    # in #221) is the OUTER group, not a subcommand of `app.cli`. It
    # discovers `app.cli` commands via `FlaskGroup.create_app`, so
    # asserting it's reachable via `app.cli.commands` would be
    # circular. Exclude from the wire-up assertion.
    declared -= {cli_mod.mimir}

    missing = declared - attached
    assert not missing, (
        "module-level click.Command(s) not reachable via `app.cli` after "
        f"`register_cli(app)`: {sorted(c.name for c in missing)}. "
        "Either add to register_cli() or attach to an existing subgroup."
    )


def test_mimir_entry_point_help_lists_registered_commands():
    """#221: the standalone `mimir` Click group (Poetry console
    script `mimir`) exposes the same commands `register_cli` attaches
    to `app.cli`. Hits `--help` via `CliRunner` so the test doesn't
    depend on the entry-point being on $PATH inside the test runner."""
    from mimir.cli import mimir as mimir_group
    result = CliRunner().invoke(mimir_group, ["--help"])
    assert result.exit_code == 0
    # Spot-check a few commands across the surface: a top-level
    # operator command, a backfill, the admin group, a Flask builtin
    # (FlaskGroup hands those through too).
    for cmd in ("ingest", "backfill-patch-series", "admin", "run"):
        assert cmd in result.output, (
            f"`mimir --help` is missing `{cmd}`:\n{result.output}"
        )
