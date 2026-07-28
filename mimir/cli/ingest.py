"""lkml-side upstream pulls: `ingest`, `reindex`, `update`, plus the
IndexNow push hook the `update` scheduler tick fires after a
successful ingest.

`update` is the one-shot "discover + clone + fetch + ingest" the
scheduler sidecar fires on a cron; `ingest` and `reindex` are the
sub-pieces an operator may want to drive directly.

The mainline-tree pull (`update-mainline`) is a separate concern
and lives in `mimir.cli.mainline`.

`sync_epochs` is imported at module scope so tests can monkeypatch
it; `ingest_inbox` is invoked indirectly via the broker RPC, no
test stub is needed.
"""

import logging
from pathlib import Path

import click
from sqlalchemy import delete, update

from mimir import indexnow
from mimir.cli._common import _EPOCH_RE, _configure_logging, _select_inboxes
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.ingest import DEFAULT_WORKERS, ingest_epoch
from mimir.models import ArticleList, IngestState
from mimir.sync import sync_epochs
from mimir.thread_roots import backfill_inbox

logger = logging.getLogger(__name__)


def _ingest_all_dispatch(
    inboxes: dict,
    limit: int | None,
    workers: int,
) -> dict:
    """Per-inbox ingest via broker RPC.

    Each `ingest_inbox` call goes through the broker, which runs
    the work in its own writer process. Cross-inbox `--limit`
    semantics are preserved: limit decrements as inboxes complete;
    the loop stops once exhausted. Result shape:
    `{inbox_name: [IngestResult, ...]}`.
    """
    from mimir.broker.client import BrokerUnavailable, get_broker_client

    client = get_broker_client()
    out: dict = {}
    remaining = limit
    for name in inboxes:
        if remaining is not None and remaining <= 0:
            break
        try:
            results = client.ingest_inbox(
                name,
                limit=remaining,
                workers=workers,
            )
        except BrokerUnavailable as exc:
            raise click.ClickException(f"broker ingest_inbox({name}) failed: {exc}")
        out[name] = results
        if remaining is not None:
            for r in results:
                remaining -= r.new + r.linked + r.dup_batch + r.dup_db + r.failed
    return out


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
    results_by_name = _ingest_all_dispatch(
        inboxes=inboxes,
        limit=limit,
        workers=workers,
    )
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
    inbox_name: str,
    epoch: str,
    from_scratch: bool,
    verbose: int,
    workers: int,
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

    if from_scratch:
        # Fail BEFORE destroying anything. `ingest_epoch` resolves its
        # writer from the broker context, which only `serve()` ever
        # sets, so a plain CLI process raises `RuntimeError("No active
        # broker")` the moment the re-walk starts. That is pre-existing
        # (this command has been unusable outside the broker since the
        # single-writer migration), but the destructive half runs and
        # COMMITS first, so without this check a `--from-scratch` in the
        # wrong process deletes an epoch's links, blanks the inbox's
        # thread roots, and then dies before it can put anything back.
        #
        # Nothing repairs that afterwards: the startup backfill is
        # sentinel-gated and the sentinel already exists post-deploy,
        # the scheduler has no thread-roots pass, and
        # `verify_thread_roots` only samples non-NULL rows so it is
        # structurally blind to an all-NULL inbox. The sitemap would
        # silently drop every thread in that inbox until a human
        # noticed. Refusing up front is the whole fix.
        from mimir.broker._context import get_active_writer

        try:
            get_active_writer()
        except RuntimeError:
            raise click.ClickException(
                "reindex --from-scratch needs an active broker writer, and this "
                "process has none, so it would delete rows it cannot rebuild. "
                "Run it inside the broker process, or re-walk non-destructively "
                f"with `mimir reindex {inbox_name} {epoch}` (no --from-scratch)."
            )

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
            click.echo(
                f"deleted {deleted} existing inbox-links for {inbox_name}/{epoch}"
            )
            # Reset the whole inbox's materialised roots, not just this
            # epoch's. Deleting one epoch's link rows breaks the parent
            # chain for descendants living in OTHER epochs: they keep a
            # `thread_root_id` pointing at a root the recursive walk can
            # no longer reach, because the hop through the removed
            # message is gone. `find_thread_root`'s fast path re-checks
            # that the ROOT is still a member, which it is, so it would
            # keep answering with it and disagree with the walk. That
            # surfaces as a permanent 301 from a survivor's thread view
            # to a thread that no longer contains it, which crawlers
            # then cache.
            #
            # Scoping the reset to the affected descendants would mean a
            # downward walk from every deleted article; NULLing the inbox
            # is one statement and lands in the safe state the column was
            # designed around (NULL means "not yet computed", so readers
            # fall back to the CTE: correct, just slower). The backfill
            # after the re-walk restores the fast path.
            #
            # State the blast radius plainly, because "readers fall back"
            # undersells it: the sitemap's root test IS the column, so
            # between here and the backfill below this inbox contributes
            # ZERO thread URLs to its sitemap. The `finally` around the
            # re-walk is what bounds that window to this command even
            # when the re-walk fails.
            #
            # Conditional on having actually deleted something: a
            # mistyped epoch (valid name, never ingested) reports
            # "deleted 0" and must not then blank the inbox.
            if deleted:
                session.execute(
                    update(ArticleList)
                    .where(ArticleList.inbox_id == inbox.id)
                    .values(thread_root_id=None)
                )

        state = session.get(IngestState, (inbox.id, epoch))
        if state is not None:
            state.last_commit_sha = None
        session.commit()

        try:
            result = ingest_epoch(session, inbox, epoch, epoch_path, workers=workers)
        finally:
            if from_scratch and deleted:
                # Re-fill what the reset cleared, in a `finally` because
                # the failure path is the one that matters: a re-walk
                # that dies on a bad blob would otherwise leave the whole
                # inbox unrooted with nothing scheduled to repair it.
                # Every pass only touches NULL rows, so running this
                # after a partial re-walk is safe and simply roots
                # whatever did land.
                session.rollback()
                counts = backfill_inbox(session, inbox.id)
                session.commit()
                click.echo(
                    f"thread roots rebuilt for {inbox_name}: "
                    f"seeded={counts['seeded']} propagated={counts['propagated']} "
                    f"cycles={counts['cycles_broken']}"
                )
                if counts["exhausted"]:
                    # Same reasoning as `mimir backfill-thread-roots`:
                    # without a non-zero exit a truncated rebuild prints
                    # the same summary as a complete one and reads as
                    # done. It matters more here, because this command is
                    # what created the NULLs.
                    raise click.ClickException(
                        f"thread-root rebuild for {inbox_name} hit the pass "
                        "budget; rows remain unrooted, re-run "
                        "`mimir backfill-thread-roots`"
                    )

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
@click.option(
    "--skip-clone", is_flag=True, help="Don't fetch the manifest or clone new epochs."
)
@click.option(
    "--skip-fetch", is_flag=True, help="Don't `git fetch` existing local epochs."
)
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

    results_by_name = _ingest_all_dispatch(
        inboxes=inboxes,
        limit=None,
        workers=workers,
    )
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
            len(message_ids),
            cap,
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
