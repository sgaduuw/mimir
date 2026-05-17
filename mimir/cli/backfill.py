"""One-shot walkers that re-derive per-article fields from blob
bytes after the extractor lands.

All three commands (`backfill-article-files`,
`backfill-article-trailers`, `backfill-patch-series`) share the
same shape: newest-first, idempotent, mirror-unreachable rows
skipped rather than failed.
"""
import click

from mimir import patch_series, patches, trailers
from mimir.cli._common import _configure_logging


@click.command("backfill-article-files")
@click.option(
    "--limit", type=int, default=None,
    help="Cap the number of articles examined this session.",
)
@click.option(
    "--reprocess", is_flag=True,
    help="Re-extract for articles that already have rows (deletes "
         "existing rows first). Use after an extractor change.",
)
@click.option(
    "-v", "--verbose", count=True,
    help="-v: progress every batch.",
)
def backfill_article_files_command(
    limit: int | None, reprocess: bool, verbose: int,
) -> None:
    """One-shot walker that fills `article_files` for articles
    ingested before the extractor landed.

    Newest-first so a `--limit`-bounded session covers the most-
    visible articles first. Idempotent: articles with existing
    rows are skipped unless `--reprocess`. Mirror-unreachable
    articles are skipped (not failed); a re-run from a host that
    has the mirror picks them up.
    """
    _configure_logging(verbose)
    progress_fn = None
    if verbose:
        def progress_fn(r):  # noqa: E306
            click.echo(
                f"... examined={r.examined} indexed={r.indexed} "
                f"no_diff={r.no_diff} skipped={r.skipped} failed={r.failed}"
            )
    result = patches.backfill_article_files(
        limit=limit, reprocess=reprocess, progress=progress_fn,
    )
    click.echo(
        f"backfill complete: examined={result.examined} "
        f"indexed={result.indexed} no_diff={result.no_diff} "
        f"skipped={result.skipped} failed={result.failed}"
    )


@click.command("backfill-article-trailers")
@click.option(
    "--limit", type=int, default=None,
    help="Cap the number of articles examined this session.",
)
@click.option(
    "--reprocess", is_flag=True,
    help="Re-extract for articles that already have rows (deletes "
         "existing rows first). Use after an extractor change.",
)
@click.option(
    "-v", "--verbose", count=True,
    help="-v: progress every batch.",
)
def backfill_article_trailers_command(
    limit: int | None, reprocess: bool, verbose: int,
) -> None:
    """One-shot walker that fills `article_trailers` for articles
    ingested before the extractor landed.

    Mirrors `backfill-article-files`: newest-first, idempotent,
    mirror-unreachable rows skipped (not failed).
    """
    _configure_logging(verbose)
    progress_fn = None
    if verbose:
        def progress_fn(r):  # noqa: E306
            click.echo(
                f"... examined={r.examined} indexed={r.indexed} "
                f"no_trailers={r.no_trailers} skipped={r.skipped} "
                f"failed={r.failed}"
            )
    result = trailers.backfill_article_trailers(
        limit=limit, reprocess=reprocess, progress=progress_fn,
    )
    click.echo(
        f"backfill complete: examined={result.examined} "
        f"indexed={result.indexed} no_trailers={result.no_trailers} "
        f"skipped={result.skipped} failed={result.failed}"
    )


@click.command("backfill-patch-series")
@click.option(
    "--limit", type=int, default=None,
    help="Cap the number of articles examined this session.",
)
@click.option(
    "--reprocess", is_flag=True,
    help="Re-detect for articles whose key is already set (clears "
         "stale rows that no longer parse as cover letters).",
)
@click.option(
    "-v", "--verbose", count=True,
    help="-v: progress every batch.",
)
def backfill_patch_series_command(
    limit: int | None, reprocess: bool, verbose: int,
) -> None:
    """One-shot walker that fills `patch_series_key` and
    `patch_series_version` on articles ingested before the
    cover-letter detector landed.

    Cheaper than the article-files backfill: only reads
    subject + author, no body re-parse via git mirror.
    Idempotent, articles whose key is set are skipped unless
    `--reprocess`. Newest-first walk.
    """
    _configure_logging(verbose)
    progress_fn = None
    if verbose:
        def progress_fn(r):  # noqa: E306
            click.echo(
                f"... examined={r.examined} indexed={r.indexed} "
                f"in_series_indexed={r.in_series_indexed} "
                f"in_series_orphan={r.in_series_orphan} "
                f"not_cover={r.not_cover} skipped={r.skipped}"
            )
    result = patch_series.backfill_patch_series(
        limit=limit, reprocess=reprocess, progress=progress_fn,
    )
    click.echo(
        f"backfill complete: examined={result.examined} "
        f"indexed={result.indexed} "
        f"in_series_indexed={result.in_series_indexed} "
        f"in_series_orphan={result.in_series_orphan} "
        f"not_cover={result.not_cover} skipped={result.skipped}"
    )
