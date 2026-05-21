"""`admin canonicals …`: backfill `articles.canonical_inbox_id` from
the original To/Cc headers.

Use after initial deployment of canonical resolution, or after editing
an `Inbox.list_address` to repoint canonicals. Newest-first,
idempotent, resumable.

When `BROKER_SOCKET_PATH` is set, dispatches via the broker as a
chain of chunked RPCs (Phase 2.2 cooperative scheduling); see
`mimir.cli.backfill` for the chunk/resume contract.
"""
import click

from mimir.cli._common import _broker_backfill_loop, _configure_logging
from mimir.cli.admin import admin_group
from mimir.config import settings
from mimir.ingest import backfill_canonicals


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
    help="-v: progress every 1000 articles (direct mode only; broker "
         "mode logs to the broker process).",
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
    if settings.broker_socket_path is not None:
        if verbose:
            click.echo(
                "broker mode: per-batch progress flows via broker logs "
                "(e.g. `podman logs -f mimir-broker`)",
                err=True,
            )
        from mimir.broker.client import BrokerUnavailable, get_broker_client
        client = get_broker_client()
        try:
            result = _broker_backfill_loop(
                client.backfill_canonicals,
                limit=limit, reprocess=reprocess,
                extra={"inbox_filter": inbox_filter},
            )
        except BrokerUnavailable as exc:
            raise click.ClickException(
                f"broker backfill_canonicals failed: {exc}"
            )
    else:
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
