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

Runs are resumable: the broker keeps each run's cursor, so after a
broker restart or a killed CLI the same command continues where the
run stopped (`--restart --yes` starts over). See `mimir.backfill_runs`.
"""

import click

from mimir.cli._common import (
    _broker_backfill_loop,
    _configure_logging,
    _echo_run_summary,
    _verbose_broker_hint,
    backfill_options,
)


@click.command("backfill-article-files")
@click.option(
    "--patch-subjects",
    is_flag=True,
    help="Read only articles whose subject is an in-series patch "
    "([PATCH 2/5]). Solo [PATCH] mails and non-series quilt patches are "
    "left alone. Runs apart from one without this flag. With --reprocess, "
    "only those patches are redone.",
)
@backfill_options(
    reprocess_help="Re-extract for articles that already have rows (deletes "
    "existing rows first). Use after an extractor change.",
)
def backfill_article_files_command(
    patch_subjects: bool,
    limit: int | None,
    reprocess: bool,
    restart: bool,
    yes: bool,
    verbose: int,
) -> None:
    """One-shot walker that fills `article_files` for articles
    ingested before the extractor landed."""
    _configure_logging(verbose)
    _verbose_broker_hint(verbose)
    from mimir.broker.client import BrokerUnavailable, get_broker_client
    from mimir.broker.protocol import BackfillArticleFilesRequest
    from mimir.patches import BackfillResult

    client = get_broker_client()
    try:
        result, run = _broker_backfill_loop(
            client,
            BackfillArticleFilesRequest,
            BackfillResult,
            limit=limit,
            reprocess=reprocess,
            restart=restart,
            yes=yes,
            extra={"patch_subjects": patch_subjects},
        )
    except BrokerUnavailable as exc:
        raise click.ClickException(f"broker backfill_article_files failed: {exc}")
    click.echo(
        f"this session: examined={result.examined} "
        f"indexed={result.indexed} no_diff={result.no_diff} "
        f"skipped={result.skipped} failed={result.failed}"
    )
    _echo_run_summary(run)


@click.command("backfill-article-trailers")
@backfill_options(
    reprocess_help="Re-extract for articles that already have rows (deletes "
    "existing rows first). Use after an extractor change.",
)
def backfill_article_trailers_command(
    limit: int | None,
    reprocess: bool,
    restart: bool,
    yes: bool,
    verbose: int,
) -> None:
    """One-shot walker that fills `article_trailers` for articles
    ingested before the extractor landed."""
    _configure_logging(verbose)
    _verbose_broker_hint(verbose)
    from mimir.broker.client import BrokerUnavailable, get_broker_client
    from mimir.broker.protocol import BackfillArticleTrailersRequest
    from mimir.trailers import BackfillResult

    client = get_broker_client()
    try:
        result, run = _broker_backfill_loop(
            client,
            BackfillArticleTrailersRequest,
            BackfillResult,
            limit=limit,
            reprocess=reprocess,
            restart=restart,
            yes=yes,
        )
    except BrokerUnavailable as exc:
        raise click.ClickException(f"broker backfill_article_trailers failed: {exc}")
    click.echo(
        f"this session: examined={result.examined} "
        f"indexed={result.indexed} no_trailers={result.no_trailers} "
        f"skipped={result.skipped} failed={result.failed}"
    )
    _echo_run_summary(run)


@click.command("backfill-patch-series")
@backfill_options(
    reprocess_help="Re-detect for articles whose key is already set (clears "
    "stale rows that no longer parse as cover letters, and rewrites stored "
    "versions after a parser change).",
)
def backfill_patch_series_command(
    limit: int | None,
    reprocess: bool,
    restart: bool,
    yes: bool,
    verbose: int,
) -> None:
    """One-shot walker that fills `patch_series_key` and
    `patch_series_version` on articles ingested before the
    cover-letter detector landed."""
    _configure_logging(verbose)
    _verbose_broker_hint(verbose)
    from mimir.broker.client import BrokerUnavailable, get_broker_client
    from mimir.broker.protocol import BackfillPatchSeriesRequest
    from mimir.patch_series import BackfillResult

    client = get_broker_client()
    try:
        result, run = _broker_backfill_loop(
            client,
            BackfillPatchSeriesRequest,
            BackfillResult,
            limit=limit,
            reprocess=reprocess,
            restart=restart,
            yes=yes,
        )
    except BrokerUnavailable as exc:
        raise click.ClickException(f"broker backfill_patch_series failed: {exc}")
    click.echo(
        f"this session: examined={result.examined} "
        f"indexed={result.indexed} "
        f"in_series_indexed={result.in_series_indexed} "
        f"in_series_orphan={result.in_series_orphan} "
        f"not_cover={result.not_cover} reply_cover={result.reply_cover} "
        f"reply_patch={result.reply_patch} "
        f"skipped={result.skipped} "
        f"subjects_unfolded={result.subjects_unfolded}"
    )
    _echo_run_summary(run)


@click.command("backfill-series-chains")
@backfill_options(reprocess_help=None)
def backfill_series_chains_command(
    limit: int | None,
    restart: bool,
    yes: bool,
    verbose: int,
) -> None:
    """One-shot walker that splits keys holding unrelated postings under
    one cover title, by writing `patch_series_head_id`. Needed once, for
    history ingested before 3.25.0."""
    _configure_logging(verbose)
    _verbose_broker_hint(verbose)
    from mimir.broker.client import BrokerUnavailable, get_broker_client
    from mimir.broker.protocol import BackfillSeriesChainsRequest
    from mimir.patch_series_chains import BackfillResult

    client = get_broker_client()
    try:
        result, run = _broker_backfill_loop(
            client,
            BackfillSeriesChainsRequest,
            BackfillResult,
            limit=limit,
            restart=restart,
            yes=yes,
        )
    except BrokerUnavailable as exc:
        raise click.ClickException(f"broker backfill_series_chains failed: {exc}")
    click.echo(
        f"this session: examined={result.examined} "
        f"keys_chained={result.keys_chained} keys_split={result.keys_split} "
        f"heads_planned={result.heads_planned} "
        f"versions_planned={result.versions_planned} skipped={result.skipped}"
    )
    _echo_run_summary(run)


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
    from mimir.broker.protocol import BackfillThreadRootsRequest
    from mimir.extensions import SessionLocal
    from mimir.models import Inbox
    from mimir.thread_roots import verify_thread_roots

    _configure_logging(0)
    client = get_broker_client()
    counts = client.call(BackfillThreadRootsRequest(inbox=inbox))
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
