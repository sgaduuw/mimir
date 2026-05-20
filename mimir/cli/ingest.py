"""lkml-side upstream pulls: `ingest`, `reindex`, `update`, plus the
IndexNow push hook the `update` scheduler tick fires after a
successful ingest.

`update` is the one-shot "discover + clone + fetch + ingest" the
scheduler sidecar fires on a cron; `ingest` and `reindex` are the
sub-pieces an operator may want to drive directly.

The mainline-tree pull (`update-mainline`) is a separate concern
and lives in `mimir.cli.mainline`.

The two `monkeypatch` test surfaces in `test_cli.py` target this
module's `sync_epochs` and `ingest_all` names; that's why these
imports stay at module scope (so they can be replaced) and aren't
deferred into the commands' bodies.
"""
import logging
from pathlib import Path

import click
from sqlalchemy import delete

from mimir import indexnow
from mimir.cli._common import _EPOCH_RE, _configure_logging, _select_inboxes
from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.ingest import (
    DEFAULT_WORKERS,
    ingest_all,
    ingest_epoch,
)
from mimir.models import ArticleList, IngestState
from mimir.sync import sync_epochs

logger = logging.getLogger(__name__)


def _ingest_all_dispatch(
    inboxes: dict,
    limit: int | None,
    workers: int,
) -> dict:
    """Per-inbox ingest with broker-mode dispatch.

    When `settings.broker_socket_path` is set, each `ingest_inbox`
    call goes through the broker RPC (Phase 2.1). The broker
    handler runs the work in the broker process and writes through
    its own writer connection; the scheduler-tasks container's
    direct SQLite writer no longer competes with the broker's
    cache.set commits.

    Otherwise (broker mode off), falls back to the direct
    `mimir.ingest.orchestrate.ingest_all` path, which is the
    pre-Phase-2 behaviour.

    Cross-inbox `--limit` semantics are preserved: limit decrements
    as inboxes complete; the loop stops once exhausted. Result
    shape matches `ingest_all`: `{inbox_name: [IngestResult, ...]}`.
    """
    if settings.broker_socket_path is None:
        return ingest_all(inboxes=inboxes, limit=limit, workers=workers)

    from mimir.broker.client import BrokerUnavailable, get_broker_client
    client = get_broker_client()
    out: dict = {}
    remaining = limit
    for name in inboxes:
        if remaining is not None and remaining <= 0:
            break
        try:
            results = client.ingest_inbox(
                name, limit=remaining, workers=workers,
            )
        except BrokerUnavailable as exc:
            # Hard fail: this is the scheduler-side ingest loop,
            # silently falling back to direct writes would re-
            # introduce the cross-process contention this phase
            # was built to eliminate. Surface to the operator.
            raise click.ClickException(
                f"broker ingest_inbox({name}) failed: {exc}"
            )
        out[name] = results
        if remaining is not None:
            for r in results:
                remaining -= (
                    r.new + r.linked + r.dup_batch + r.dup_db + r.failed
                )
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
        inboxes=inboxes, limit=limit, workers=workers,
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

    results_by_name = _ingest_all_dispatch(
        inboxes=inboxes, limit=None, workers=workers,
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
