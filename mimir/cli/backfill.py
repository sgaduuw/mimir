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
