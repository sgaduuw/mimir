import logging
import time
from pathlib import Path

import click
from flask import Flask
from sqlalchemy import delete

from mimir.config import settings
from mimir.dashboard import archive_stats, daily_volume
from mimir.extensions import Base, SessionLocal, engine
from mimir.ingest import DEFAULT_WORKERS, ingest_all, ingest_epoch
from mimir.models import Article, IngestState
from mimir.store import MessageNotFound, read_message
from mimir.sync import sync_epochs
from mimir.threading import active_threads


def _configure_logging(verbose: int) -> None:
    if verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")


@click.command("init-db")
def init_db_command() -> None:
    """Create tables. Use alembic for real migrations; this is for quick local dev."""
    Base.metadata.create_all(engine)
    click.echo("schema created")


@click.command("ingest")
@click.option(
    "--mirror",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Path to a public-inbox mirror dir containing N.git epochs.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after processing this many messages across all epochs (for testing).",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="-v: progress every 100 messages. -vv: per-message detail. Always shows parse failures.",
)
@click.option(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    show_default=True,
    help="Parallel parsers (process pool). Set to 1 for sequential.",
)
def ingest_command(mirror: Path | None, limit: int | None, verbose: int, workers: int) -> None:
    """Walk a public-inbox mirror and import new messages."""
    _configure_logging(verbose)
    mirror_path = mirror or settings.lkml_mirror_path
    results = ingest_all(mirror_path, limit=limit, workers=workers)
    for r in results:
        click.echo(
            f"{r.epoch}: new={r.new} skipped={r.skipped} failed={r.failed} "
            f"head={r.last_commit_sha}"
        )


@click.command("reindex")
@click.argument("epoch")
@click.option(
    "--from-scratch",
    is_flag=True,
    help="Delete this epoch's existing rows before re-walking. Without "
         "this flag, just rewinds IngestState and lets dedup skip messages "
         "already saved (useful for backfilling parse failures).",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="-v: progress every 100 messages. -vv: per-message detail.",
)
@click.option(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    show_default=True,
    help="Parallel parsers (process pool). Set to 1 for sequential.",
)
def reindex_command(epoch: str, from_scratch: bool, verbose: int, workers: int) -> None:
    """Re-walk a single epoch from the beginning.

    By default, picks up messages that previously failed to parse — the
    in-DB dedup skips already-saved Message-IDs so only the new/recovered
    ones are written. Pass --from-scratch for a destructive rebuild.
    """
    _configure_logging(verbose)

    epoch_path = settings.lkml_mirror_path / epoch
    if not epoch_path.exists():
        raise click.ClickException(f"epoch repo not found: {epoch_path}")

    with SessionLocal() as session:
        if from_scratch:
            deleted = session.execute(
                delete(Article).where(Article.epoch == epoch)
            ).rowcount
            click.echo(f"deleted {deleted} existing articles for {epoch}")

        state = session.get(IngestState, epoch)
        if state is not None:
            state.last_commit_sha = None
        session.commit()

        result = ingest_epoch(session, epoch, epoch_path, workers=workers)

    click.echo(
        f"{result.epoch}: new={result.new} skipped={result.skipped} "
        f"failed={result.failed} head={result.last_commit_sha}"
    )


@click.command("show")
@click.argument("message_id")
@click.option("--body-chars", type=int, default=2000, help="Truncate body output (-1 for full).")
@click.option("--no-body", is_flag=True, help="Skip the body; useful for inspecting threading state alone.")
def show_command(message_id: str, body_chars: int, no_body: bool) -> None:
    """Fetch and pretty-print one article by Message-ID.

    Shows DB-side fields (epoch, commit_sha, indexed date, thread_parent
    and whether it's in the archive) alongside the freshly re-parsed blob
    (full headers, body, attachments). Designed for threading debug.
    """
    from sqlalchemy import select
    from mimir.models import Article

    with SessionLocal() as session:
        article = session.execute(
            select(Article).where(Article.message_id == message_id)
        ).scalar_one_or_none()
        if article is None:
            raise click.ClickException(f"no article with message_id={message_id!r}")

        try:
            parsed = read_message(session, message_id)
        except MessageNotFound as exc:
            raise click.ClickException(str(exc))

        # Resolve threading state: where does our parent point, and is it in DB?
        parent_present = None
        if article.thread_parent:
            parent_present = session.execute(
                select(Article.id).where(Article.message_id == article.thread_parent)
            ).scalar_one_or_none() is not None

    click.echo("--- DB row ---")
    click.echo(f"id:            {article.id}")
    click.echo(f"epoch:         {article.epoch}")
    click.echo(f"commit_sha:    {article.commit_sha}")
    click.echo(f"date:          {article.date.isoformat() if article.date else ''}")
    click.echo(f"in_reply_to:   {article.in_reply_to or '(none)'}")
    click.echo(f"references:    {article.references or '(none)'}")
    click.echo(
        f"thread_parent: {article.thread_parent or '(none)'}"
        + (f"  [in DB: {parent_present}]" if article.thread_parent else "")
    )
    click.echo()
    click.echo("--- parsed blob ---")
    click.echo(f"Message-ID: {parsed.message_id}")
    click.echo(f"From:       {parsed.author or ''}")
    click.echo(f"Date:       {parsed.date.isoformat() if parsed.date else ''}")
    click.echo(f"Subject:    {parsed.subject or ''}")
    if parsed.in_reply_to:
        click.echo(f"In-Reply-To: {parsed.in_reply_to}")
    if parsed.references:
        click.echo(f"References: {' '.join(parsed.references)}")
    for a in parsed.attachments:
        click.echo(f"Attachment: {a.filename or '(no name)'} [{a.content_type}] {len(a.content)} bytes")
    if no_body:
        return
    click.echo()
    if parsed.body:
        body = parsed.body if body_chars < 0 else parsed.body[:body_chars]
        click.echo(body)
        if body_chars >= 0 and len(parsed.body) > body_chars:
            click.echo(f"\n... ({len(parsed.body) - body_chars} more chars truncated; pass --body-chars=-1 for full)")


@click.command("update")
@click.option(
    "--mirror",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Mirror directory. Defaults to settings.lkml_mirror_path.",
)
@click.option(
    "--upstream",
    type=str,
    default=None,
    help="Upstream public-inbox root URL. Defaults to settings.upstream_url.",
)
@click.option("--skip-clone", is_flag=True, help="Don't fetch the manifest or clone new epochs.")
@click.option("--skip-fetch", is_flag=True, help="Don't `git fetch` existing local epochs.")
@click.option("--skip-ingest", is_flag=True, help="Don't run ingest after sync.")
@click.option(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    show_default=True,
    help="Parallel parsers for the ingest stage.",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="-v: progress every 100 messages. -vv: per-message detail.",
)
def update_command(
    mirror: Path | None,
    upstream: str | None,
    skip_clone: bool,
    skip_fetch: bool,
    skip_ingest: bool,
    workers: int,
    verbose: int,
) -> None:
    """Discover new upstream epochs, fetch updates, and ingest in one shot."""
    _configure_logging(verbose)
    mirror_path = mirror or settings.lkml_mirror_path
    upstream_url = upstream or settings.upstream_url

    sync_result = sync_epochs(
        upstream_url,
        mirror_path,
        discover_new=not skip_clone,
        fetch_existing=not skip_fetch,
    )
    click.echo(
        f"sync: cloned={sync_result.cloned} fetched={sync_result.fetched} "
        f"failed={sync_result.failed}"
    )

    if skip_ingest:
        return

    ingest_results = ingest_all(mirror_path, workers=workers)
    for r in ingest_results:
        click.echo(
            f"{r.epoch}: new={r.new} skipped={r.skipped} failed={r.failed} "
            f"head={r.last_commit_sha}"
        )


@click.command("warm-cache")
def warm_cache_command() -> None:
    """Recompute and cache the slow dashboard queries.

    Designed to run from cron or a systemd timer. Refreshes the on-disk
    cache (`Settings.cache_path`) so the Flask server picks up
    pre-computed results on the next request, avoiding the cold-start
    latency on `/`.

    Example crontab (every 5 min for active threads, daily for stats):

        */5 * * * * cd ~/Projects/python/mimir && poetry run flask --app mimir warm-cache
    """
    targets = [
        ("active_threads (7d, 10)", lambda s: active_threads(s, days=7, limit=10, force=True)),
        ("daily_volume (30d)", lambda s: daily_volume(s, days=30, force=True)),
        ("archive_stats", lambda s: archive_stats(s, force=True)),
    ]
    with SessionLocal() as session:
        for label, fn in targets:
            t0 = time.perf_counter()
            fn(session)
            click.echo(f"{label}: {(time.perf_counter() - t0) * 1000:.0f} ms")


def register_cli(app: Flask) -> None:
    app.cli.add_command(init_db_command)
    app.cli.add_command(ingest_command)
    app.cli.add_command(reindex_command)
    app.cli.add_command(show_command)
    app.cli.add_command(update_command)
    app.cli.add_command(warm_cache_command)
