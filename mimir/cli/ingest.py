"""Upstream-pull commands: `ingest`, `reindex`, `update`,
`update-mainline`, plus the IndexNow push hook and the mainline
tree helpers.

`update` is the one-shot "discover + clone + fetch + ingest" that
the scheduler sidecar fires on a cron; `ingest` and `reindex` are
the sub-pieces an operator may want to drive directly. The mainline
flow walks Linus's `linux.git`, replaces the subsystems schema
from MAINTAINERS, and indexes commit `Link:` trailers.

The two `monkeypatch` test surfaces in `test_cli.py` target this
module's `sync_epochs` and `ingest_all` names; that's why these
imports stay at module scope (so they can be replaced) and aren't
deferred into the commands' bodies.
"""
import logging
import subprocess
from pathlib import Path

import click
from sqlalchemy import delete

from mimir import indexnow, maintainers
from mimir.cli._common import _EPOCH_RE, _configure_logging, _select_inboxes
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.ingest import (
    DEFAULT_WORKERS,
    ingest_all,
    ingest_epoch,
)
from mimir.models import (
    ArticleList,
    IngestState,
    MainlineState,
    Subsystem,
    SubsystemMaintainer,
    SubsystemPath,
)
from mimir.sync import sync_epochs

logger = logging.getLogger(__name__)


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

    By default, picks up messages that previously failed to parse, the
    in-DB dedup skips already-saved Message-IDs so only the new/recovered
    ones are written. Pass --from-scratch for a destructive rebuild.
    """
    _configure_logging(verbose)

    if not _EPOCH_RE.fullmatch(epoch):
        raise click.BadParameter(
            f"epoch must be of the form 'N.git' (got {epoch!r})",
            param_hint="EPOCH",
        )
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

    # Default verbosity prints only lines that signal a state change.
    # Steady-state ticks on a settled archive (sync: 0/0/0 across the
    # board, ingest: dup_batch/dup_db only) become silent, anything
    # in the log then means something actually happened. -v restores
    # the per-inbox / per-epoch lines unconditionally.
    for name, inbox in inboxes.items():
        sync_result = sync_epochs(
            inbox.upstream_url,
            Path(inbox.mirror_path),
            discover_new=not skip_clone,
            fetch_existing=not skip_fetch,
        )
        if verbose or sync_result.cloned or sync_result.fetched or sync_result.failed:
            click.echo(
                f"{name} sync: cloned={sync_result.cloned} "
                f"fetched={sync_result.fetched} failed={sync_result.failed}"
            )

    if skip_ingest:
        return

    results_by_name = ingest_all(inboxes=inboxes, workers=workers)
    new_message_ids: list[str] = []
    for name, results in results_by_name.items():
        for r in results:
            new_message_ids.extend(r.new_message_ids)
            if verbose or r.new or r.linked or r.failed:
                click.echo(
                    f"{name}/{r.epoch}: new={r.new} linked={r.linked} "
                    f"dup_batch={r.dup_batch} dup_db={r.dup_db} "
                    f"failed={r.failed} head={r.last_commit_sha}"
                )

    _push_indexnow(new_message_ids)


def _push_indexnow(message_ids: list[str]) -> None:
    """Best-effort IndexNow push for an update tick. No-op when the
    feature isn't configured; skip-with-warning when the per-tick
    count exceeds `indexnow_max_per_tick` (fresh-deploy or post-
    outage catch-up shouldn't act like a backfill, the sitemap
    handles the backlog naturally on Bing's regular crawl)."""
    if not message_ids or not settings.indexnow_key:
        return
    cap = settings.indexnow_max_per_tick
    if len(message_ids) > cap:
        logger.warning(
            "indexnow: %d new URLs this tick exceeds INDEXNOW_MAX_PER_TICK=%d "
            "  skipping push, relying on sitemap",
            len(message_ids), cap,
        )
        return
    base = (settings.site_base_url or "").rstrip("/")
    if not base:
        logger.warning(
            "indexnow: key set but SITE_BASE_URL empty, cannot build URLs, "
            "skipping push"
        )
        return
    with SessionLocal() as session:
        urls = indexnow.build_urls(session, message_ids, base=base)
    submitted = indexnow.notify(urls)
    # State-change line at default verbosity: the per-epoch
    # `name/epoch: new=N ...` lines emit via click.echo at the same
    # level for the same reason, "anything in the scheduler log
    # signals a real event." The INFO log inside `notify` stays put
    # for `-v` operators who want the per-chunk status detail.
    if submitted:
        click.echo(f"indexnow: pushed {submitted} URL(s)")


@click.command("update-mainline")
@click.option(
    "--skip-fetch", is_flag=True,
    help="Don't `git fetch` the mainline mirror; just re-read the local HEAD.",
)
@click.option(
    "--skip-maintainers", is_flag=True,
    help="Don't re-parse MAINTAINERS; only walk new commits for Link: trailers.",
)
@click.option(
    "--skip-commits", is_flag=True,
    help="Don't walk commits for Link: trailers; only reload MAINTAINERS.",
)
@click.option(
    "--force", is_flag=True,
    help="Re-parse MAINTAINERS and replace subsystems even if HEAD hasn't moved.",
)
@click.option(
    "-v", "--verbose", count=True,
    help="-v: progress detail. -vv: debug.",
)
def update_mainline_command(
    skip_fetch: bool, skip_maintainers: bool, skip_commits: bool,
    force: bool, verbose: int,
) -> None:
    """Sync the mainline kernel tree (Linus's `linux.git`) and load
    its MAINTAINERS file + index `Link:` trailers from commit
    messages.

    Clones the tree on first run, fetches on subsequent runs. Two
    independent passes against the resulting tree:

    1. MAINTAINERS: read the file at HEAD, replace the
       `subsystems` / `subsystem_paths` / `subsystem_maintainers`
       tables transactionally. Skipped when HEAD hasn't moved
       since the last load (`--force` overrides; for re-runs
       after a parser fix). `--skip-maintainers` disables this
       pass entirely for the tick.

    2. Commit Link-trailer walk: scan every commit since the last
       walker cursor for `Link: https://lore.kernel.org/.../<msgid>`
       trailers, insert into `mainline_commits`. Resumable; the
       second cursor on `MainlineState` advances monotonically.
       First run walks the full history (slow); subsequent ticks
       only see new commits. `--skip-commits` disables this pass.

    The two passes have independent cursors because MAINTAINERS
    only changes when that one file does, but commit-walker has
    new work on almost every tick.
    """
    _configure_logging(verbose)
    tree_path = Path(settings.mainline_tree_path)
    if not tree_path.is_absolute():
        from mimir.config import PROJECT_ROOT
        tree_path = PROJECT_ROOT / tree_path

    if not tree_path.exists():
        tree_path.parent.mkdir(parents=True, exist_ok=True)
        click.echo(f"cloning {settings.mainline_tree_url} -> {tree_path}")
        # `--` stops git from interpreting an option-shaped URL as a
        # flag (same defensive pattern as `mimir.sync.clone_epoch`).
        subprocess.run(
            ["git", "clone", "--mirror", "--",
             settings.mainline_tree_url, str(tree_path)],
            check=True,
        )
    elif not skip_fetch:
        if verbose:
            click.echo(f"fetching {tree_path}")
        subprocess.run(
            ["git", "-C", str(tree_path), "fetch", "--quiet", "--prune"],
            check=True,
        )

    tree_name = "linus"  # fixed slug for now; supports future linux-stable, etc.

    if not skip_maintainers:
        _load_maintainers(tree_path, tree_name, force)

    if not skip_commits:
        _walk_link_trailers(tree_path, tree_name)


def _load_maintainers(tree_path: Path, tree_name: str, force: bool) -> None:
    """Read MAINTAINERS from HEAD and replace the subsystems
    schema. No-op when HEAD matches `last_commit_sha` and not
    forced."""
    from dulwich.repo import Repo
    repo = Repo(str(tree_path))
    head_sha = repo.head().decode("ascii")
    commit = repo[repo.head()]
    tree = repo[commit.tree]
    try:
        _mode, blob_sha = tree[b"MAINTAINERS"]
    except KeyError:
        raise click.ClickException(
            f"no MAINTAINERS file at HEAD of {tree_path}; wrong tree?"
        )
    blob_bytes = repo[blob_sha].data

    with SessionLocal() as session:
        state = session.get(MainlineState, tree_name)
        if state is None:
            state = MainlineState(tree_name=tree_name)
            session.add(state)
        if state.last_commit_sha == head_sha and not force:
            click.echo(
                f"update-mainline: MAINTAINERS unchanged (HEAD "
                f"{head_sha[:12]}); use --force to re-parse"
            )
            return

        parsed = maintainers.parse(blob_bytes)
        # Replace-all in one transaction. The cascade FK on
        # `subsystems.id` clears `subsystem_paths` + `subsystem_maintainers`
        # via ON DELETE CASCADE; SQLite needs `PRAGMA foreign_keys=ON`
        # (set by `mimir.extensions` on every connection) for the
        # cascade to fire.
        session.execute(delete(Subsystem))
        for sub in parsed:
            row = Subsystem(name=sub.name, status=sub.status)
            for path in sub.files:
                row.paths.append(SubsystemPath(glob=path, is_exclude=False))
            for path in sub.excludes:
                row.paths.append(SubsystemPath(glob=path, is_exclude=True))
            for m in sub.maintainers:
                row.maintainers.append(SubsystemMaintainer(
                    role=m.role, name=m.name, address=m.address,
                ))
            session.add(row)
        state.last_commit_sha = head_sha
        session.commit()

    # Invalidate the dynamic-allowlist cache so the web tier picks
    # up the refreshed M:/R: address set on the next request rather
    # than serving stale redaction decisions for up to the cache
    # TTL. The cache table is shared across processes, so this
    # delete from the scheduler sidecar reaches the web container
    # too.
    from mimir import maintainer_allowlist
    maintainer_allowlist.invalidate()

    click.echo(
        f"update-mainline: loaded {len(parsed)} subsystems "
        f"from {tree_name}@{head_sha[:12]}"
    )


def _walk_link_trailers(tree_path: Path, tree_name: str) -> None:
    """Walk new commits, extract `Link:` trailers, insert
    `mainline_commits` rows. Resumable via the cursor on
    `MainlineState.commits_walked_to_sha`."""
    from mimir import mainline
    with SessionLocal() as session:
        result = mainline.walk_commits(session, tree_path, tree_name=tree_name)
    # State-change line at default verbosity. Steady-state ticks
    # produce zero new commits and stay silent.
    if result.commits_seen:
        click.echo(
            f"update-mainline: walked {result.commits_seen} commits "
            f"({result.linked} with lore Link:, {result.rows_inserted} "
            "rows indexed)"
        )
