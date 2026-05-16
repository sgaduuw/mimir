"""Quick local-dev schema bootstrap. Real migrations go through
alembic; this command is the "just give me an empty DB" shortcut for
fresh checkouts.
"""
import click

from mimir.extensions import Base, engine


@click.command("init-db")
def init_db_command() -> None:
    """Create tables. Use alembic for real migrations; this is for quick local dev."""
    Base.metadata.create_all(engine)
    click.echo("schema created")
