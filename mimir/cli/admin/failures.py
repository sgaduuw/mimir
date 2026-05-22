"""`admin failures …`: list and replay persisted parse failures.

Every commit whose `m` blob couldn't be parsed during ingest lands in
`parse_failures` keyed by (inbox, epoch, commit_sha). Once the parser
is fixed, `replay` re-fetches the blob, re-runs the parser, and on
success inserts the article + deletes the row. Failed replays bump
`attempts` and `last_attempt`.
"""

import click
from sqlalchemy import func, select

from mimir.cli.admin import admin_group
from mimir.extensions import SessionLocal
from mimir.models import Inbox, ParseFailure


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
@click.option(
    "--inbox",
    "inbox_filter",
    type=str,
    default=None,
    help="Restrict to one inbox by name.",
)
@click.option(
    "--epoch",
    "epoch_filter",
    type=str,
    default=None,
    help="Restrict to one epoch (e.g. 0.git). Implies --inbox.",
)
@click.option(
    "--error-class",
    "error_class",
    type=str,
    default=None,
    help="Restrict to one exception class (e.g. MessageTooLarge).",
)
@click.option(
    "--limit",
    type=int,
    default=50,
    show_default=True,
    help="Cap the number of rows shown. Use 0 for no cap.",
)
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
        total_q = (
            select(func.count())
            .select_from(ParseFailure)
            .join(Inbox, Inbox.id == ParseFailure.inbox_id)
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
@click.option(
    "--epoch",
    "epoch_filter",
    type=str,
    default=None,
    help="Restrict to one epoch (e.g. 0.git).",
)
@click.option(
    "--limit", type=int, default=None, help="Cap on rows replayed. Default: all."
)
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
    # The broker handler looks the inbox up server-side and runs
    # `replay_failures` inside the broker process.
    from mimir.broker.client import BrokerUnavailable, get_broker_client

    try:
        payload = get_broker_client().failures_replay(
            inbox_name,
            epoch_filter=epoch_filter,
            limit=limit,
        )
    except BrokerUnavailable as exc:
        # Translate the broker's `InboxNotFound:<msg>` structured
        # error back into the operator-facing text.
        raw = str(exc)
        _, _, reply_error = raw.partition(": ")
        if reply_error.startswith("InboxNotFound:"):
            raise click.ClickException(reply_error[len("InboxNotFound:") :])
        raise click.ClickException(raw)
    click.echo(
        f"{inbox_name}: attempted={payload.get('attempted', 0)} "
        f"recovered={payload.get('recovered', 0)} "
        f"still_failed={payload.get('still_failed', 0)} "
        f"skipped={payload.get('skipped', 0)}"
    )
