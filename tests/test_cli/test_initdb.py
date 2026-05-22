"""Tests for mimir/cli/initdb.py: the dev-bootstrap `init-db`
command (alembic upgrade head against a fresh DB path)."""

from click.testing import CliRunner

from mimir.cli import (
    init_db_command,
)


def test_init_db_command_runs_and_creates_schema(tmp_path, monkeypatch):
    """`flask --app mimir init-db` calls `Base.metadata.create_all`
    against the configured engine. Hard to test in-place (the
    engine is module-global and already migrated), so point it at
    a throwaway sqlite file via the engine's URL.

    Pin two things:
    1. Exit code 0 and "schema created" output (operator UX).
    2. The expected tables actually exist on the fresh file
       afterwards. A regression that swapped `create_all` for a
       no-op would still print "schema created"; the table check
       catches it.
    """
    import sqlalchemy
    from mimir.extensions import Base
    from mimir import extensions as ext_module
    from mimir.cli import initdb as cli_module

    fresh_db = tmp_path / "fresh-init-db.sqlite"
    fresh_url = f"sqlite:///{fresh_db}"
    fresh_engine = sqlalchemy.create_engine(fresh_url, future=True)

    # The CLI module captured `engine` at import time, so monkeypatching
    # `mimir.extensions.engine` doesn't reach it. Patch the cli module's
    # own bound name too.
    monkeypatch.setattr(ext_module, "engine", fresh_engine)
    monkeypatch.setattr(cli_module, "engine", fresh_engine)

    result = CliRunner().invoke(init_db_command, [])
    assert result.exit_code == 0, result.output
    assert "schema created" in result.output

    # Tables exist on the fresh DB.
    inspector = sqlalchemy.inspect(fresh_engine)
    tables = set(inspector.get_table_names())
    expected = {t.name for t in Base.metadata.tables.values()}
    missing = expected - tables
    assert not missing, (
        f"init-db should have created every ORM table; missing {missing}"
    )


# `admin canonicals backfill` -- no failure shape to assert; just
# pin that the wrapper runs, exits 0, and emits the summary line.
