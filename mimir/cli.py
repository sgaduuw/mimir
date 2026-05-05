import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from flask import Flask
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from mimir.config import settings
from mimir.dashboard import (
    archive_stats,
    author_recent,
    daily_volume,
    latest_pull_requests,
    latest_stable_releases,
    this_day_in_history,
)
from mimir.extensions import Base, SessionLocal, engine
from mimir.inboxes import (
    InboxNotFound,
    InboxValidationError,
    bootstrap_inboxes,
    create_inbox,
    delete_inbox,
    get_inbox,
    list_inboxes,
    update_inbox,
)
from mimir.ingest import DEFAULT_WORKERS, ingest_all, ingest_epoch
from mimir.models import Article, ArticleList, Inbox, IngestState
from mimir.store import MessageNotFound, read_message
from mimir.sync import sync_epochs
from mimir.threading import active_threads, threads_for_day


def _configure_logging(verbose: int) -> None:
    if verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")


def _select_inboxes(only: str | None) -> dict[str, Inbox]:
    """Bootstrap from env, then narrow to one inbox name if provided."""
    inboxes = bootstrap_inboxes()
    if only is None:
        return inboxes
    if only not in inboxes:
        raise click.ClickException(f"unknown inbox: {only!r}")
    return {only: inboxes[only]}


@click.command("init-db")
def init_db_command() -> None:
    """Create tables. Use alembic for real migrations; this is for quick local dev."""
    Base.metadata.create_all(engine)
    click.echo("schema created")


@click.command("ingest")
@click.option(
    "--inbox",
    "inbox_filter",
    type=str,
    default=None,
    help="Restrict to one configured inbox by name. Default: all configured inboxes.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Stop after processing this many messages total (for testing).",
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
def ingest_command(
    inbox_filter: str | None,
    limit: int | None,
    verbose: int,
    workers: int,
) -> None:
    """Walk every configured inbox's mirror and import new messages."""
    _configure_logging(verbose)
    inboxes = _select_inboxes(inbox_filter)
    results_by_name = ingest_all(inboxes=inboxes, limit=limit, workers=workers)
    for name, results in results_by_name.items():
        for r in results:
            click.echo(
                f"{name}/{r.epoch}: new={r.new} linked={r.linked} "
                f"dup_batch={r.dup_batch} dup_db={r.dup_db} "
                f"failed={r.failed} head={r.last_commit_sha}"
            )


@click.command("reindex")
@click.argument("inbox_name")
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
def reindex_command(
    inbox_name: str, epoch: str, from_scratch: bool, verbose: int, workers: int,
) -> None:
    """Re-walk a single epoch in INBOX from the beginning.

    By default, picks up messages that previously failed to parse — the
    in-DB dedup skips already-saved Message-IDs so only the new/recovered
    ones are written. Pass --from-scratch for a destructive rebuild.
    """
    _configure_logging(verbose)

    inboxes = _select_inboxes(inbox_name)
    inbox = inboxes[inbox_name]
    epoch_path = Path(inbox.mirror_path) / epoch
    if not epoch_path.exists():
        raise click.ClickException(f"epoch repo not found: {epoch_path}")

    with SessionLocal() as session:
        # Re-attach the detached Inbox bootstrap_inboxes returned.
        inbox = session.merge(inbox)

        if from_scratch:
            # Drop the per-inbox link rows. Articles themselves stay (they
            # may also be linked from other inboxes via cross-posts); the
            # re-walk will re-add ArticleList rows.
            deleted = session.execute(
                delete(ArticleList).where(
                    ArticleList.inbox_id == inbox.id,
                    ArticleList.epoch == epoch,
                )
            ).rowcount
            click.echo(f"deleted {deleted} existing inbox-links for {inbox_name}/{epoch}")

        state = session.get(IngestState, (inbox.id, epoch))
        if state is not None:
            state.last_commit_sha = None
        session.commit()

        result = ingest_epoch(session, inbox, epoch, epoch_path, workers=workers)

    click.echo(
        f"{inbox_name}/{result.epoch}: new={result.new} linked={result.linked} "
        f"dup_batch={result.dup_batch} dup_db={result.dup_db} "
        f"failed={result.failed} head={result.last_commit_sha}"
    )


@click.command("show")
@click.argument("message_id")
@click.option(
    "--inbox",
    "inbox_filter",
    type=str,
    default=None,
    help="Read the blob from this inbox's mirror. Default: first linked inbox.",
)
@click.option("--body-chars", type=int, default=2000, help="Truncate body output (-1 for full).")
@click.option("--no-body", is_flag=True, help="Skip the body; useful for inspecting threading state alone.")
def show_command(
    message_id: str,
    inbox_filter: str | None,
    body_chars: int,
    no_body: bool,
) -> None:
    """Fetch and pretty-print one article by Message-ID.

    Shows DB-side fields (inboxes it's linked to, indexed date, thread_parent
    and whether it's in the archive) alongside the freshly re-parsed blob
    (full headers, body, attachments). Designed for threading debug.
    """
    bootstrap_inboxes()
    with SessionLocal() as session:
        article = session.execute(
            select(Article).where(Article.message_id == message_id)
        ).scalar_one_or_none()
        if article is None:
            raise click.ClickException(f"no article with message_id={message_id!r}")

        links = session.execute(
            select(ArticleList)
            .where(ArticleList.article_id == article.id)
            .options(selectinload(ArticleList.inbox))
        ).scalars().all()
        if not links:
            raise click.ClickException(f"article {message_id!r} has no inbox links")

        if inbox_filter is not None:
            chosen = next(
                (link for link in links if link.inbox.name == inbox_filter), None
            )
            if chosen is None:
                raise click.ClickException(
                    f"article not linked to inbox {inbox_filter!r}; "
                    f"linked to: {[link.inbox.name for link in links]}"
                )
        else:
            chosen = links[0]

        try:
            parsed = read_message(session, chosen.inbox, message_id)
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
    click.echo(
        f"linked inboxes:{', '.join(f'{link.inbox.name}/{link.epoch}@{link.commit_sha[:10]}' for link in links)}"
    )
    click.echo(f"reading from:  {chosen.inbox.name}")
    click.echo(f"date:          {article.date.isoformat() if article.date else ''}")
    click.echo(f"thread_parent: {article.thread_parent or '(none)'}"
               + (f"  [in DB: {parent_present}]" if article.thread_parent else ""))
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
    "--inbox",
    "inbox_filter",
    type=str,
    default=None,
    help="Restrict to one configured inbox by name. Default: all configured inboxes.",
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
    inbox_filter: str | None,
    skip_clone: bool,
    skip_fetch: bool,
    skip_ingest: bool,
    workers: int,
    verbose: int,
) -> None:
    """Discover new upstream epochs, fetch updates, and ingest in one shot.
    Iterates over every configured inbox unless --inbox restricts to one."""
    _configure_logging(verbose)
    inboxes = _select_inboxes(inbox_filter)

    for name, inbox in inboxes.items():
        sync_result = sync_epochs(
            inbox.upstream_url,
            Path(inbox.mirror_path),
            discover_new=not skip_clone,
            fetch_existing=not skip_fetch,
        )
        click.echo(
            f"{name} sync: cloned={sync_result.cloned} "
            f"fetched={sync_result.fetched} failed={sync_result.failed}"
        )

    if skip_ingest:
        return

    results_by_name = ingest_all(inboxes=inboxes, workers=workers)
    for name, results in results_by_name.items():
        for r in results:
            click.echo(
                f"{name}/{r.epoch}: new={r.new} linked={r.linked} "
                f"dup_batch={r.dup_batch} dup_db={r.dup_db} "
                f"failed={r.failed} head={r.last_commit_sha}"
            )


@click.command("warm-cache")
def warm_cache_command() -> None:
    """Recompute and cache the slow dashboard queries for every inbox.

    Designed to run from cron or a systemd timer. Refreshes the
    DB-backed `cache` table so the Flask server picks up pre-computed
    results on the next request, avoiding cold-start latency.

    Example crontab:

        */5 * * * * cd ~/Projects/python/mimir && poetry run flask --app mimir warm-cache
    """
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    inboxes = bootstrap_inboxes()
    with SessionLocal() as session:
        targets = []
        for inbox in inboxes.values():
            targets.extend([
                (f"{inbox.name} active_threads (7d, 10)",
                 lambda s, ib=inbox: active_threads(s, ib, days=7, limit=10, force=True)),
                (f"{inbox.name} threads_for_day (today)",
                 lambda s, ib=inbox: threads_for_day(s, ib, today, force=True)),
                (f"{inbox.name} threads_for_day (yesterday)",
                 lambda s, ib=inbox: threads_for_day(s, ib, yesterday, force=True)),
                (f"{inbox.name} daily_volume (30d)",
                 lambda s, ib=inbox: daily_volume(s, ib, days=30, force=True)),
                (f"{inbox.name} archive_stats",
                 lambda s, ib=inbox: archive_stats(s, ib, force=True)),
                (f"{inbox.name} latest_pull_requests",
                 lambda s, ib=inbox: latest_pull_requests(s, ib, limit=5, force=True)),
                (f"{inbox.name} latest_stable_releases",
                 lambda s, ib=inbox: latest_stable_releases(s, ib, limit=5, force=True)),
                (f"{inbox.name} this_day_in_history",
                 lambda s, ib=inbox: this_day_in_history(s, ib, years_ago=5, limit=3, force=True)),
            ])
            for label, substr in settings.tracked_authors.items():
                targets.append((
                    f"{inbox.name} tracker:{label}",
                    lambda s, ib=inbox, sub=substr: author_recent(s, ib, sub, 5, force=True),
                ))
        for label, fn in targets:
            t0 = time.perf_counter()
            fn(session)
            click.echo(f"{label}: {(time.perf_counter() - t0) * 1000:.0f} ms")


@click.group("admin")
def admin_group() -> None:
    """Administrative operations on the underlying data."""


@admin_group.group("inbox")
def admin_inbox_group() -> None:
    """CRUD on the `inboxes` table.

    These commands are the CLI front-end to the same service-layer
    functions the future Flask admin UI will call. Validation,
    cascade-delete semantics, and the nav-name cache refresh all live
    in `mimir.inboxes`.
    """


@admin_inbox_group.command("list")
def admin_inbox_list_command() -> None:
    """List every configured inbox with its mirror path and upstream URL."""
    inboxes = list_inboxes()
    if not inboxes:
        click.echo("(no inboxes)")
        return
    name_w = max(len(ix.name) for ix in inboxes)
    path_w = max(len(ix.mirror_path) for ix in inboxes)
    for ix in inboxes:
        click.echo(
            f"{ix.id:>4}  {ix.name:<{name_w}}  "
            f"{ix.mirror_path:<{path_w}}  {ix.upstream_url}"
        )


@admin_inbox_group.command("show")
@click.argument("name")
def admin_inbox_show_command(name: str) -> None:
    """Detail view for one inbox: config + per-epoch ingest cursors."""
    try:
        inbox = get_inbox(name)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))

    click.echo(f"id:           {inbox.id}")
    click.echo(f"name:         {inbox.name}")
    click.echo(f"mirror_path:  {inbox.mirror_path}")
    click.echo(f"upstream_url: {inbox.upstream_url}")

    with SessionLocal() as session:
        states = session.execute(
            select(IngestState).where(IngestState.inbox_id == inbox.id)
            .order_by(IngestState.epoch)
        ).scalars().all()
        article_count = session.execute(
            select(func.count()).select_from(ArticleList)
            .where(ArticleList.inbox_id == inbox.id)
        ).scalar_one()
    click.echo(f"linked articles: {article_count}")
    if states:
        click.echo("ingest cursors:")
        for s in states:
            head = s.last_commit_sha or "<beginning>"
            click.echo(f"  {s.epoch}: {head}")
    else:
        click.echo("ingest cursors: (none — never ingested)")


@admin_inbox_group.command("add")
@click.argument("name")
@click.option("--mirror-path", required=True, help="Filesystem path to the public-inbox mirror root.")
@click.option("--upstream-url", required=True, help="Upstream public-inbox URL (https://...).")
def admin_inbox_add_command(name: str, mirror_path: str, upstream_url: str) -> None:
    """Insert a new inbox. Run `flask --app mimir update --inbox <name>`
    afterwards to clone the upstream mirror and ingest."""
    try:
        inbox = create_inbox(name, mirror_path=mirror_path, upstream_url=upstream_url)
    except InboxValidationError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"created inbox {inbox.name!r} (id={inbox.id})")
    click.echo(
        f"next: poetry run flask --app mimir update --inbox {inbox.name}"
    )


@admin_inbox_group.command("update")
@click.argument("name")
@click.option("--mirror-path", default=None, help="New filesystem path.")
@click.option("--upstream-url", default=None, help="New upstream URL.")
@click.option("--rename", "new_name", default=None,
              help="Rename to NEW_NAME (changes URL slug + cache keys).")
def admin_inbox_update_command(
    name: str,
    mirror_path: str | None,
    upstream_url: str | None,
    new_name: str | None,
) -> None:
    """Modify an existing inbox. Only the supplied fields are touched."""
    if mirror_path is None and upstream_url is None and new_name is None:
        raise click.ClickException(
            "nothing to update — pass at least one of "
            "--mirror-path / --upstream-url / --rename"
        )
    try:
        inbox = update_inbox(
            name,
            new_name=new_name,
            mirror_path=mirror_path,
            upstream_url=upstream_url,
        )
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))
    except InboxValidationError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"updated inbox {inbox.name!r}")
    click.echo(f"  mirror_path:  {inbox.mirror_path}")
    click.echo(f"  upstream_url: {inbox.upstream_url}")


@admin_inbox_group.command("remove")
@click.argument("name")
@click.option(
    "--keep-orphan-articles",
    is_flag=True,
    help="Keep articles that lose their last inbox link. Default is to "
         "delete them (other inboxes' cross-posts are unaffected).",
)
@click.option(
    "--remove-inbox-data",
    is_flag=True,
    help="Also delete the on-disk public-inbox mirror at <mirror_path>. "
         "Permanent — re-cloning takes hours and ~20 GB for lkml. Prompts "
         "for confirmation.",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def admin_inbox_remove_command(
    name: str,
    keep_orphan_articles: bool,
    remove_inbox_data: bool,
    yes: bool,
) -> None:
    """Delete an inbox and its dependent rows.

    Cascades: removes article_lists + ingest_state rows for this inbox.
    By default also deletes any articles left without remaining links.
    Cross-posts to other inboxes are unaffected.
    """
    try:
        inbox = get_inbox(name)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))

    if remove_inbox_data:
        path = Path(inbox.mirror_path)
        target = path.parent if path.name == "git" else path
        click.echo(f"--remove-inbox-data set: will rm -rf {target}")
        if target.exists() and not yes:
            click.confirm(
                f"DELETE the on-disk mirror at {target}?",
                abort=True,
            )

    if not yes:
        click.confirm(
            f"Remove inbox {name!r} from the database?", abort=True,
        )

    report = delete_inbox(
        name,
        keep_orphan_articles=keep_orphan_articles,
        remove_inbox_data=remove_inbox_data,
    )
    click.echo(f"removed inbox {report.name!r}")
    click.echo(f"  article_lists rows deleted: {report.article_lists_deleted}")
    click.echo(f"  ingest_state rows deleted:  {report.ingest_state_deleted}")
    if not keep_orphan_articles:
        click.echo(f"  orphan articles deleted:    {report.orphan_articles_deleted}")
    if report.mirror_path_deleted:
        click.echo(f"  removed on-disk mirror:     {report.mirror_path_deleted}")


def register_cli(app: Flask) -> None:
    app.cli.add_command(init_db_command)
    app.cli.add_command(ingest_command)
    app.cli.add_command(reindex_command)
    app.cli.add_command(show_command)
    app.cli.add_command(update_command)
    app.cli.add_command(warm_cache_command)
    app.cli.add_command(admin_group)
