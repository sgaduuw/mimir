"""One-shot walkers that re-derive per-article fields from blob
bytes after the extractor lands.

All three commands (`backfill-article-files`,
`backfill-article-trailers`, `backfill-patch-series`) share the
same shape: newest-first, idempotent, mirror-unreachable rows
skipped rather than failed.

Each command dispatches via the broker as a chain of chunked RPCs
(Phase 2.2 cooperative scheduling). The CLI aggregates per-chunk
counters back into the same summary line the operator expects;
per-batch `--verbose` progress flows via the broker's own log
(`podman logs -f mimir-broker`) since the broker is the one doing
the walking. The chunk seconds dial is
`BROKER_BACKFILL_CHUNK_SECONDS` (default 10 s); shorter dials
yield finer interleaving with queued cache writes / ingest ticks.
"""

import click

from mimir.cli._common import (
    _broker_backfill_loop,
    _configure_logging,
    _verbose_broker_hint,
)


@click.command("backfill-article-files")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap the number of articles examined this session.",
)
@click.option(
    "--reprocess",
    is_flag=True,
    help="Re-extract for articles that already have rows (deletes "
    "existing rows first). Use after an extractor change.",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="-v: progress hint (broker mode logs to the broker process; "
    "use `podman logs -f mimir-broker` to follow).",
)
def backfill_article_files_command(
    limit: int | None,
    reprocess: bool,
    verbose: int,
) -> None:
    """One-shot walker that fills `article_files` for articles
    ingested before the extractor landed."""
    _configure_logging(verbose)
    _verbose_broker_hint(verbose)
    from mimir.broker.client import BrokerUnavailable, get_broker_client

    client = get_broker_client()
    try:
        result = _broker_backfill_loop(
            client.backfill_article_files,
            limit=limit,
            reprocess=reprocess,
        )
    except BrokerUnavailable as exc:
        raise click.ClickException(f"broker backfill_article_files failed: {exc}")
    click.echo(
        f"backfill complete: examined={result.examined} "
        f"indexed={result.indexed} no_diff={result.no_diff} "
        f"skipped={result.skipped} failed={result.failed}"
    )


@click.command("backfill-article-trailers")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap the number of articles examined this session.",
)
@click.option(
    "--reprocess",
    is_flag=True,
    help="Re-extract for articles that already have rows (deletes "
    "existing rows first). Use after an extractor change.",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="-v: progress hint (broker mode logs to the broker process; "
    "use `podman logs -f mimir-broker` to follow).",
)
def backfill_article_trailers_command(
    limit: int | None,
    reprocess: bool,
    verbose: int,
) -> None:
    """One-shot walker that fills `article_trailers` for articles
    ingested before the extractor landed."""
    _configure_logging(verbose)
    _verbose_broker_hint(verbose)
    from mimir.broker.client import BrokerUnavailable, get_broker_client

    client = get_broker_client()
    try:
        result = _broker_backfill_loop(
            client.backfill_article_trailers,
            limit=limit,
            reprocess=reprocess,
        )
    except BrokerUnavailable as exc:
        raise click.ClickException(f"broker backfill_article_trailers failed: {exc}")
    click.echo(
        f"backfill complete: examined={result.examined} "
        f"indexed={result.indexed} no_trailers={result.no_trailers} "
        f"skipped={result.skipped} failed={result.failed}"
    )


@click.command("backfill-patch-series")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap the number of articles examined this session.",
)
@click.option(
    "--reprocess",
    is_flag=True,
    help="Re-detect for articles whose key is already set (clears "
    "stale rows that no longer parse as cover letters).",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="-v: progress hint (broker mode logs to the broker process; "
    "use `podman logs -f mimir-broker` to follow).",
)
def backfill_patch_series_command(
    limit: int | None,
    reprocess: bool,
    verbose: int,
) -> None:
    """One-shot walker that fills `patch_series_key` and
    `patch_series_version` on articles ingested before the
    cover-letter detector landed."""
    _configure_logging(verbose)
    _verbose_broker_hint(verbose)
    from mimir.broker.client import BrokerUnavailable, get_broker_client

    client = get_broker_client()
    try:
        result = _broker_backfill_loop(
            client.backfill_patch_series,
            limit=limit,
            reprocess=reprocess,
        )
    except BrokerUnavailable as exc:
        raise click.ClickException(f"broker backfill_patch_series failed: {exc}")
    click.echo(
        f"backfill complete: examined={result.examined} "
        f"indexed={result.indexed} "
        f"in_series_indexed={result.in_series_indexed} "
        f"in_series_orphan={result.in_series_orphan} "
        f"not_cover={result.not_cover} skipped={result.skipped}"
    )


@click.command("backfill-thread-roots")
@click.option(
    "--inbox",
    default=None,
    help="Only this inbox. Default: every configured inbox.",
)
@click.option(
    "--verify",
    is_flag=True,
    # Names the CTE deliberately: the oracle must NOT be
    # `find_thread_root`, which reads the column and would agree with
    # any corruption by construction.
    help="After filling, recompute a sample of roots against the "
    "recursive CTE and report any that disagree.",
)
@click.option(
    "--sample",
    type=int,
    default=200,
    help="Rows per inbox to recompute under --verify.",
)
def backfill_thread_roots_command(
    inbox: str | None,
    verify: bool,
    sample: int,
) -> None:
    """Populate `article_lists.thread_root_id` for rows predating it.

    Idempotent and resumable: only NULL rows are touched, so a re-run
    picks up where an interrupted one stopped and never clobbers what
    live ingest wrote alongside it. Readers fall back to the recursive
    CTE wherever the column is still NULL, so a partially-filled corpus
    is correct, just not yet fast.

    `--verify` is the important half. The failure mode this column
    introduces is invisible: a maintenance bug splits a conversation in
    two, both halves render fine, and nothing errors. Only recomputing
    catches it, so verify after a backfill and periodically thereafter.
    """
    from sqlalchemy import select

    from mimir.broker.client import get_broker_client
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from mimir.thread_roots import verify_thread_roots

    _configure_logging(0)
    client = get_broker_client()
    counts = client.backfill_thread_roots(inbox=inbox)
    if inbox and not counts.get("inboxes"):
        # A typo'd inbox matched nothing. Reporting the usual summary
        # would read as "done" when nothing ran.
        raise click.ClickException(f"no such inbox: {inbox}")
    click.echo(
        "thread-roots: {inboxes} inbox(es), {seeded} seeded, "
        "{propagated} propagated, {cycles_broken} cycle(s) broken".format(
            inboxes=counts.get("inboxes", 0),
            seeded=counts.get("seeded", 0),
            propagated=counts.get("propagated", 0),
            cycles_broken=counts.get("cycles_broken", 0),
        )
    )

    if counts.get("exhausted"):
        # The log line only reaches the broker's stdout; without this
        # a truncated backfill returns the same summary as a complete
        # one and reads as done.
        raise click.ClickException(
            f"{counts['exhausted']} inbox(es) hit the pass budget; "
            "rows remain unrooted. Re-run to continue."
        )

    if not verify:
        return

    total = 0
    with SessionLocal() as session:
        stmt = select(Inbox)
        if inbox:
            stmt = stmt.where(Inbox.name == inbox)
        for ix in session.execute(stmt).scalars():
            mismatches = verify_thread_roots(session, ix, limit=sample)
            total += len(mismatches)
            for m in mismatches[:10]:
                click.echo(
                    f"  MISMATCH {ix.name} {m['message_id']}: "
                    f"stored={m['stored_root']} expected={m['expected_root']}"
                )
    if total:
        raise click.ClickException(
            f"{total} thread-root mismatch(es); the column disagrees with "
            "find_thread_root, which means maintenance is wrong"
        )
    click.echo("thread-roots: verified, no mismatches")
