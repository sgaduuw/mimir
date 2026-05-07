import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from flask import Flask
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import selectinload

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
    add_tracked_author,
    bootstrap_inboxes,
    clear_tracked_authors,
    create_inbox,
    delete_inbox,
    get_inbox,
    list_inboxes,
    remove_tracked_author,
    set_tracked_authors,
    update_inbox,
)
from mimir.ingest import (
    DEFAULT_WORKERS,
    backfill_canonicals,
    ingest_all,
    ingest_epoch,
    replay_failures,
)
from mimir.models import Article, ArticleList, Inbox, IngestState, ParseFailure
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
            for label, substr in (inbox.tracked_authors or {}).items():
                targets.append((
                    f"{inbox.name} tracker:{label}",
                    lambda s, ib=inbox, sub=substr: author_recent(s, ib, sub, 5, force=True),
                ))
        for label, fn in targets:
            t0 = time.perf_counter()
            fn(session)
            click.echo(f"{label}: {(time.perf_counter() - t0) * 1000:.0f} ms")

    # Drop expired rows so the cache table doesn't grow monotonically
    # as search keys, dated `threads_for_day:...:YYYY-MM-DD` keys, and
    # `monthly_volume:<inbox>:<year>` keys for past years accumulate.
    from mimir import cache as _cache
    purged = _cache.purge_expired()
    if purged:
        click.echo(f"purged {purged} expired cache row{'' if purged == 1 else 's'}")


def _fmt_bytes(n: int) -> str:
    """Human-readable byte size for the vacuum / status output."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@click.command("analyze")
def analyze_command() -> None:
    """Run ANALYZE to refresh the SQLite query planner statistics.

    Stale `sqlite_stat1` makes the planner pick bad plans (we hit
    this once when the migration's ANALYZE ran on empty tables and
    later ingest left the stats wrong by orders of magnitude). Run
    after a big ingest delta — daily or weekly via cron is plenty.

    Example crontab (4:30am, after the daily VACUUM):

        30 4 * * * cd ~/Projects/python/mimir && poetry run flask --app mimir analyze
    """
    t0 = time.perf_counter()
    with engine.begin() as conn:
        conn.execute(text("ANALYZE"))
    elapsed = time.perf_counter() - t0
    click.echo(f"ANALYZE complete in {elapsed:.1f} s")


@click.command("vacuum")
def vacuum_command() -> None:
    """Reclaim space: VACUUM + WAL checkpoint.

    SQLite never reclaims freed pages on its own; the .db file grows
    past its actual content over time, and the WAL file grows during
    long ingests until something checkpoints it. This command runs
    `VACUUM` (rebuilds the database into a compact form) and
    `PRAGMA wal_checkpoint(TRUNCATE)` (collapses the WAL).

    VACUUM holds an exclusive lock for the duration and needs ~2× the
    on-disk size of free space. Run it during a quiet window — typical
    cadence is daily or weekly via cron, while ingest isn't active.

    Example crontab:

        0 4 * * * cd ~/Projects/python/mimir && poetry run flask --app mimir vacuum
    """
    db_path = Path(engine.url.database) if engine.url.database else None
    if db_path is None:
        raise click.ClickException("could not resolve DB path from engine URL")

    def _sizes() -> dict[str, int]:
        out: dict[str, int] = {}
        for suffix in ("", "-wal", "-shm"):
            p = db_path.with_name(db_path.name + suffix)
            out[suffix or "db"] = p.stat().st_size if p.exists() else 0
        return out

    before = _sizes()
    total_before = before["db"] + before["-wal"] + before["-shm"]
    click.echo(
        f"before: db={_fmt_bytes(before['db'])}  "
        f"wal={_fmt_bytes(before['-wal'])}  shm={_fmt_bytes(before['-shm'])}"
    )

    # `wal_checkpoint(TRUNCATE)` only succeeds when there are no
    # other readers — SQLAlchemy's connection pool keeps idle
    # connections that block it. Dispose the pool and run on a fresh
    # raw sqlite3 connection so we own the only handle. Any other
    # process that has the DB open (web server, warm-cache cron) will
    # also prevent the truncate; stop those before vacuuming.
    import sqlite3
    engine.dispose()

    t0 = time.perf_counter()
    conn = sqlite3.connect(str(db_path))
    try:
        # In WAL mode, VACUUM's full rebuild goes through the WAL, so
        # the WAL grows by ~db_size during the operation. Checkpoint
        # *after* to collapse it; the pre-checkpoint clears any
        # leftover WAL from prior writers so the truncate can run
        # cleanly when we're done.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    elapsed = time.perf_counter() - t0

    after = _sizes()
    total_after = after["db"] + after["-wal"] + after["-shm"]
    click.echo(
        f"after:  db={_fmt_bytes(after['db'])}  "
        f"wal={_fmt_bytes(after['-wal'])}  shm={_fmt_bytes(after['-shm'])}"
    )
    reclaimed = total_before - total_after
    click.echo(
        f"reclaimed {_fmt_bytes(reclaimed)} in {elapsed:.1f} s"
    )


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
        n = len(ix.tracked_authors or {})
        trackers = f"trackers={n}" if n else "trackers=none"
        click.echo(
            f"{ix.id:>4}  {ix.name:<{name_w}}  "
            f"{ix.mirror_path:<{path_w}}  {ix.upstream_url}  {trackers}"
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


@admin_inbox_group.group("trackers")
def admin_inbox_trackers_group() -> None:
    """Per-inbox author trackers shown on the inbox dashboard.

    Each tracker is a (label, email-substring) pair; the dashboard
    renders one tile per tracker showing that author's most recent
    messages in this inbox. With no trackers configured, the section
    is hidden entirely.
    """


@admin_inbox_trackers_group.command("show")
@click.argument("name")
def admin_inbox_trackers_show_command(name: str) -> None:
    """Print the tracker dict for one inbox."""
    try:
        inbox = get_inbox(name)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))
    authors = inbox.tracked_authors or {}
    if not authors:
        click.echo(f"{inbox.name}: no trackers configured")
        return
    label_w = max(len(label) for label in authors)
    click.echo(f"{inbox.name}: {len(authors)} tracker(s)")
    for label, substring in authors.items():
        click.echo(f"  {label:<{label_w}}  {substring}")


def _parse_pair(pair: str) -> tuple[str, str]:
    if "=" not in pair:
        raise click.ClickException(
            f"expected LABEL=SUBSTRING, got {pair!r}"
        )
    label, substring = pair.split("=", 1)
    return label, substring


@admin_inbox_trackers_group.command("set")
@click.argument("name")
@click.argument("pairs", nargs=-1, required=True)
def admin_inbox_trackers_set_command(name: str, pairs: tuple[str, ...]) -> None:
    """Replace the tracker dict in one shot. Each PAIR is LABEL=SUBSTRING."""
    authors: dict[str, str] = {}
    for pair in pairs:
        label, substring = _parse_pair(pair)
        authors[label] = substring
    try:
        inbox = set_tracked_authors(name, authors)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))
    except InboxValidationError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{inbox.name}: set {len(inbox.tracked_authors or {})} tracker(s)")


@admin_inbox_trackers_group.command("add")
@click.argument("name")
@click.argument("label")
@click.argument("substring")
def admin_inbox_trackers_add_command(
    name: str, label: str, substring: str,
) -> None:
    """Add (or replace) one tracker entry."""
    try:
        inbox = add_tracked_author(name, label, substring)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))
    except InboxValidationError as exc:
        raise click.ClickException(str(exc))
    click.echo(
        f"{inbox.name}: added tracker {label!r} → {substring!r} "
        f"({len(inbox.tracked_authors or {})} total)"
    )


@admin_inbox_trackers_group.command("remove")
@click.argument("name")
@click.argument("label")
def admin_inbox_trackers_remove_command(name: str, label: str) -> None:
    """Remove one tracker entry by label."""
    try:
        inbox = remove_tracked_author(name, label)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))
    except InboxValidationError as exc:
        raise click.ClickException(str(exc))
    n = len(inbox.tracked_authors or {})
    click.echo(f"{inbox.name}: removed tracker {label!r} ({n} remaining)")


@admin_inbox_trackers_group.command("clear")
@click.argument("name")
def admin_inbox_trackers_clear_command(name: str) -> None:
    """Drop all tracker entries (writes NULL)."""
    try:
        inbox = clear_tracked_authors(name)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{inbox.name}: cleared all trackers")


@admin_group.group("failures")
def admin_failures_group() -> None:
    """List and replay persisted parse failures.

    Every commit whose `m` blob couldn't be parsed during ingest lands
    in `parse_failures` keyed by (inbox, epoch, commit_sha). Once the
    parser is fixed, `replay` re-fetches the blob, re-runs the parser,
    and on success inserts the article + deletes the row. Failed
    replays bump `attempts` and `last_attempt`.
    """


@admin_failures_group.command("list")
@click.option("--inbox", "inbox_filter", type=str, default=None,
              help="Restrict to one inbox by name.")
@click.option("--epoch", "epoch_filter", type=str, default=None,
              help="Restrict to one epoch (e.g. 0.git). Implies --inbox.")
@click.option("--error-class", "error_class", type=str, default=None,
              help="Restrict to one exception class (e.g. MessageTooLarge).")
@click.option("--limit", type=int, default=50, show_default=True,
              help="Cap the number of rows shown. Use 0 for no cap.")
def admin_failures_list_command(
    inbox_filter: str | None,
    epoch_filter: str | None,
    error_class: str | None,
    limit: int,
) -> None:
    """Print persisted parse failures, newest-attempt first."""
    if epoch_filter is not None and inbox_filter is None:
        raise click.ClickException("--epoch requires --inbox")

    with SessionLocal() as session:
        q = select(ParseFailure, Inbox.name).join(
            Inbox, Inbox.id == ParseFailure.inbox_id
        )
        if inbox_filter is not None:
            q = q.where(Inbox.name == inbox_filter)
        if epoch_filter is not None:
            q = q.where(ParseFailure.epoch == epoch_filter)
        if error_class is not None:
            q = q.where(ParseFailure.error_class == error_class)
        q = q.order_by(ParseFailure.last_attempt.desc())
        if limit > 0:
            q = q.limit(limit)
        rows = list(session.execute(q).all())

        # Aggregate counters even when --limit truncates output.
        total_q = select(func.count()).select_from(ParseFailure).join(
            Inbox, Inbox.id == ParseFailure.inbox_id
        )
        if inbox_filter is not None:
            total_q = total_q.where(Inbox.name == inbox_filter)
        if epoch_filter is not None:
            total_q = total_q.where(ParseFailure.epoch == epoch_filter)
        if error_class is not None:
            total_q = total_q.where(ParseFailure.error_class == error_class)
        total = session.execute(total_q).scalar_one()

    if not rows:
        click.echo("(no parse failures)")
        return

    for row, inbox_name in rows:
        click.echo(
            f"{inbox_name}/{row.epoch}@{row.commit_sha[:12]}  "
            f"{row.error_class}: {row.error_message[:80]}  "
            f"(attempts={row.attempts}, last={row.last_attempt.isoformat(timespec='seconds')})"
        )
    if limit > 0 and total > len(rows):
        click.echo(f"... {total - len(rows)} more (use --limit 0 to show all)")
    click.echo(f"total: {total}")


@admin_failures_group.command("replay")
@click.argument("inbox_name")
@click.option("--epoch", "epoch_filter", type=str, default=None,
              help="Restrict to one epoch (e.g. 0.git).")
@click.option("--limit", type=int, default=None,
              help="Cap on rows replayed. Default: all.")
def admin_failures_replay_command(
    inbox_name: str,
    epoch_filter: str | None,
    limit: int | None,
) -> None:
    """Re-parse persisted parse failures for INBOX_NAME.

    Successful parses insert the article and delete the failure row.
    Failed parses bump `attempts` + `last_attempt`. Use after a parser
    fix:

        flask --app mimir admin failures replay lkml
        flask --app mimir admin failures replay lkml --epoch 0.git
    """
    try:
        inbox = get_inbox(inbox_name)
    except InboxNotFound as exc:
        raise click.ClickException(str(exc))

    result = replay_failures(inbox, epoch_filter=epoch_filter, limit=limit)
    click.echo(
        f"{inbox_name}: attempted={result.attempted} "
        f"recovered={result.recovered} still_failed={result.still_failed} "
        f"skipped={result.skipped}"
    )


@admin_group.group("canonicals")
def admin_canonicals_group() -> None:
    """Canonical-inbox-related admin operations.

    `backfill` walks historical articles and resolves
    `articles.canonical_inbox_id` from the original To/Cc headers,
    emitting <link rel="canonical"> targets for cross-posts. Use after
    initial deployment of canonical resolution, or after editing an
    `Inbox.list_address` to repoint canonicals.
    """


@admin_canonicals_group.command("backfill")
@click.option("--inbox", "inbox_filter", type=str, default=None,
              help="Restrict to articles linked to this inbox.")
@click.option("--limit", type=int, default=None,
              help="Cap the number of articles examined this session.")
@click.option(
    "--reprocess",
    is_flag=True,
    help="Re-examine articles whose canonical_inbox_id is already set "
         "(use after editing list_address or to clean up the early-pass "
         "bootstrap region).",
)
@click.option(
    "-v", "--verbose", count=True,
    help="-v: progress every 1000 articles.",
)
def admin_canonicals_backfill_command(
    inbox_filter: str | None,
    limit: int | None,
    reprocess: bool,
    verbose: int,
) -> None:
    """Walk historical articles, recording per-inbox address
    observations and resolving canonical_inbox_id from original
    To/Cc headers. Newest-first, idempotent, resumable."""
    _configure_logging(verbose)
    progress_fn = None
    if verbose:
        def progress_fn(r):  # noqa: E306
            click.echo(
                f"... examined={r.examined} resolved={r.resolved} "
                f"unresolved={r.unresolved} skipped={r.skipped}"
            )
    result = backfill_canonicals(
        inbox_filter=inbox_filter,
        limit=limit,
        reprocess=reprocess,
        progress=progress_fn,
    )
    click.echo(
        f"backfill complete: examined={result.examined} "
        f"resolved={result.resolved} unresolved={result.unresolved} "
        f"skipped={result.skipped}"
    )


def register_cli(app: Flask) -> None:
    app.cli.add_command(init_db_command)
    app.cli.add_command(ingest_command)
    app.cli.add_command(reindex_command)
    app.cli.add_command(show_command)
    app.cli.add_command(update_command)
    app.cli.add_command(warm_cache_command)
    app.cli.add_command(vacuum_command)
    app.cli.add_command(analyze_command)
    app.cli.add_command(admin_group)
