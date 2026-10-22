"""`admin canonicals …`: backfill `articles.canonical_inbox_id` from
the original To/Cc headers.

Use after initial deployment of canonical resolution, or after editing
an `Inbox.list_address` to repoint canonicals. Newest-first,
idempotent, resumable.

Dispatches via the broker as a chain of chunked RPCs (Phase 2.2
cooperative scheduling); see `mimir.cli.backfill` for the
chunk/resume contract.
"""

import click

from mimir.cli._common import (
    _broker_backfill_loop,
    _configure_logging,
    _echo_run_summary,
    _verbose_broker_hint,
    backfill_options,
)
from mimir.cli.admin import admin_group


@admin_group.group("canonicals")
def admin_canonicals_group() -> None:
    """Canonical-inbox-related admin operations.

    `backfill` walks historical articles and resolves
    `articles.canonical_inbox_id` from the original To/Cc headers,
    emitting <link rel="canonical"> targets for cross-posts. Use after
    initial deployment of canonical resolution, or after editing an
    `Inbox.list_address` to repoint canonicals (with `--reprocess
    --restart`, so an open run does not resume below its old cursor;
    add `--yes` when it refuses to discard a run that has progress).
    """


@admin_canonicals_group.command("backfill")
@click.option(
    "--inbox",
    "inbox_filter",
    type=str,
    default=None,
    help="Restrict to articles linked to this inbox.",
)
@backfill_options(
    reprocess_help="Re-examine articles whose canonical_inbox_id is already set "
    "(use with --restart after editing list_address, or to clean up "
    "the early-pass bootstrap region).",
)
def admin_canonicals_backfill_command(
    inbox_filter: str | None,
    limit: int | None,
    reprocess: bool,
    restart: bool,
    yes: bool,
    verbose: int,
) -> None:
    """Walk historical articles, recording per-inbox address
    observations and resolving canonical_inbox_id from original
    To/Cc headers. Newest-first, idempotent, resumable."""
    _configure_logging(verbose)
    _verbose_broker_hint(verbose)
    from mimir.broker.client import BrokerUnavailable, get_broker_client
    from mimir.broker.protocol import BackfillCanonicalsRequest
    from mimir.ingest.backfill import BackfillResult

    client = get_broker_client()
    try:
        result, run = _broker_backfill_loop(
            client,
            BackfillCanonicalsRequest,
            BackfillResult,
            limit=limit,
            reprocess=reprocess,
            restart=restart,
            yes=yes,
            extra={"inbox_filter": inbox_filter},
        )
    except BrokerUnavailable as exc:
        raise click.ClickException(f"broker backfill_canonicals failed: {exc}")
    click.echo(
        f"this session: examined={result.examined} "
        f"resolved={result.resolved} unresolved={result.unresolved} "
        f"skipped={result.skipped}"
    )
    _echo_run_summary(run)
