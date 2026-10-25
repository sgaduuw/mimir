"""Long-op handlers. Each is a multi-second to multi-minute
operation that holds the SQLite writer lock for a non-trivial
duration: full-inbox ingest (Phase 2.1), backfills (Phase 2.2),
update-mainline (Phase 2.3), analyze, vacuum (Phase 2.3). These
route to the broker's **long worker** (the long_queue) so they
don't block cache writes serving the web tier in the meantime;
the two workers contend for the SQLite writer lock at the SQLite
level, but cache writes only wait for the long op's current
commit batch, not the whole op.

The Phase 2.2 backfill handlers add **cooperative scheduling** on
top of that: each backfill RPC runs at most
`Settings.broker_backfill_chunk_seconds` (default 10 s) before
committing what it processed and returning `partial=True` to the
caller. The CLI loops with a follow-up RPC, and between two chunks
the long worker is free to service queued cache writes / other long
ops. The broker owns the walk's cursor: each chunk resolves its run
in `backfill_runs` and starts below the run's cursor, so a killed CLI
or a broker restart loses at most the batch in flight (#680). Without this, a multi-hour
`backfill_canonicals --reprocess` would block every queued
scheduler `update` tick behind it.

Imports for `mimir.ingest.*`, `mimir.models`, etc. are deferred
into each handler body so the broker process's import-time graph
stays lean. Heavy imports only land when the first long-op RPC
actually arrives (which is typically much later than process
start, since the broker boots fast and long ops fire on the
scheduler's tick schedule).
"""

import logging

from mimir.broker.protocol import (
    BackfillArticleFilesRequest,
    BackfillArticleTrailersRequest,
    BackfillCanonicalsRequest,
    BackfillPatchSeriesRequest,
    BackfillSeriesChainsRequest,
    BootstrapInboxesRequest,
    IngestInboxRequest,
    ReindexRequest,
    Reply,
)

logger = logging.getLogger(__name__)


def handle_bootstrap_inboxes(req: BootstrapInboxesRequest) -> Reply:
    """Reconcile `Settings.inboxes` env config into the `inboxes`
    table. Delegates to `mimir.inboxes.bootstrap_inboxes()` (the
    same function the scheduler-tasks container called directly
    pre-Phase-2).

    Phase 6a: bootstrap_inboxes now dispatches its own write through
    the active WriterThread, so no shared-engine write_transaction
    wrapper is needed here."""
    from mimir.inboxes import bootstrap_inboxes

    # Phase 6a: bootstrap_inboxes now dispatches its own write through
    # the active WriterThread, so no shared-engine write_transaction
    # wrapper is needed here.
    inboxes = bootstrap_inboxes()
    return Reply(rpc_id=req.rpc_id, ok=True, result={"inboxes": len(inboxes)})


def handle_ingest_inbox(req: IngestInboxRequest) -> Reply:
    """Run `ingest_inbox` for one inbox inside the broker process.
    Looks up the Inbox row by name from the DB (do NOT call
    `bootstrap_inboxes` here, that's a write, and the caller has
    either already done it at startup or is asking the broker to
    ingest an inbox that should already exist). Returns the per-
    epoch `IngestResult` list serialised into the Reply.result
    payload as a list of dicts; the client reconstructs IngestResult
    instances so the CLI sees the same shape as the direct path.

    `ingest_inbox` already does its own `write_transaction()` calls
    internally with per-block labels (`ingest_inbox:<name>`,
    `auto_analyze:<name>`, etc.), so we don't wrap here. The slow-
    write log will show those labels uniformly between direct and
    broker-mediated runs.
    """
    from sqlalchemy import select

    from mimir.extensions import SessionLocal
    from mimir.ingest.epoch import DEFAULT_WORKERS
    from mimir.ingest.orchestrate import ingest_inbox
    from mimir.models import Inbox

    with SessionLocal() as session:
        inbox = session.execute(
            select(Inbox).where(Inbox.name == req.inbox_name)
        ).scalar_one_or_none()
    if inbox is None:
        return Reply(
            rpc_id=req.rpc_id,
            ok=False,
            error=f"UnknownInbox:{req.inbox_name}",
        )
    workers = req.workers if req.workers is not None else DEFAULT_WORKERS
    results = ingest_inbox(inbox, limit=req.limit, workers=workers)
    return Reply(
        rpc_id=req.rpc_id,
        ok=True,
        result={"results": [r.model_dump(mode="json") for r in results]},
    )


def handle_reindex(req: ReindexRequest) -> Reply:
    """Reindex on the long worker; all mutations use the single writer."""
    from mimir.ingest.epoch import DEFAULT_WORKERS
    from mimir.ingest.reindex import reindex_epoch

    try:
        result = reindex_epoch(
            req.inbox_name,
            req.epoch,
            from_scratch=req.from_scratch,
            workers=req.workers if req.workers is not None else DEFAULT_WORKERS,
        )
    except (ValueError, FileNotFoundError) as exc:
        return Reply(rpc_id=req.rpc_id, ok=False, error=str(exc))
    return Reply(rpc_id=req.rpc_id, ok=True, result=result)


def _chunk_seconds() -> float:
    """Per-chunk time budget for the backfill handlers. Read from
    settings on each call so an env override picks up without
    restarting the broker (settings is a singleton but it's safe to
    re-read attrs; the value is an int)."""
    from mimir.config import settings

    return float(settings.broker_backfill_chunk_seconds)


def _run_backfill_chunk(req, name: str, backfill, **params) -> Reply:
    """Run one chunk of backfill `name` within its run, and build the
    Reply shared by the four backfill handlers.

    `params` are the arguments that define the run, so the same values
    build the run key and drive the walk. The Reply carries the chunk's
    `BackfillResult` whole as `counters`, `partial` mirrored out so the
    CLI can branch without re-parsing the counters, and `run`: when the
    run started, the cursor this chunk started from, the examined count
    across the whole run, and whether the run is finished.

    A follow-up chunk whose run another session finished walks nothing
    and returns `partial=False`, which ends this session. So does a
    first chunk that finds the run live (another session is driving
    it): `run` is that run with `busy` set and `retry_after` seconds,
    and nothing was changed. So does a
    `restart` refused because the unfinished run has progress: `run` is
    then that run with `refused` set, and nothing was deleted. A first
    chunk that discarded a run carries it as `run["discarded"]`, and
    the broker logs it, since the deleted row was the only record."""
    from mimir import backfill_runs

    resolved = backfill_runs.resolve(
        name, backfill_runs.params_key(**params), start=req.start
    )
    run = resolved.run
    # Busy and refused both stop a first chunk with nothing run or
    # changed; the flag tells the CLI which message to give.
    if resolved.busy is not None:
        held = resolved.busy
        flag = {"busy": True, "retry_after": resolved.retry_after}
    elif resolved.refused is not None:
        held = resolved.refused
        flag = {"refused": True}
    else:
        held = None
    if held is not None:
        return Reply(
            rpc_id=req.rpc_id,
            ok=True,
            result={
                "counters": {},
                "partial": False,
                "run": {**_run_info(held, held), **flag},
            },
        )
    discarded = None
    if resolved.discarded is not None:
        discarded = _run_info(resolved.discarded, resolved.discarded)
        logger.warning(
            "%s: discarding run %d started %s UTC at id %s (%d examined)",
            name,
            discarded["id"],
            discarded["started_at"],
            discarded["cursor"],
            discarded["examined"],
        )
    if run is None or run.finished:
        return Reply(
            rpc_id=req.rpc_id,
            ok=True,
            result={"counters": {}, "partial": False, "run": _run_info(run, run)},
        )
    result = backfill(limit=req.limit, max_seconds=_chunk_seconds(), run=run, **params)
    # The CLI stops once its remaining budget is spent (`partial` or
    # not), so a chunk that spends it ends the session: release the
    # claim for the next invocation. `limit` is the remaining budget.
    if req.limit is not None and result.examined >= req.limit:
        backfill_runs.release(run.id)
    return Reply(
        rpc_id=req.rpc_id,
        ok=True,
        result={
            "counters": result.model_dump(mode="json"),
            "partial": bool(result.partial),
            # Read back, so the totals include other sessions' chunks;
            # a run deleted by `--restart` meanwhile reports as it was.
            "run": {
                **_run_info(run, backfill_runs.get(run.id) or run),
                **({"discarded": discarded} if discarded else {}),
            },
        },
    )


def _run_info(before, now) -> dict | None:
    if before is None:
        return None
    return {
        "id": before.id,
        "started_at": before.started_at.isoformat(sep=" ", timespec="seconds"),
        "cursor": before.cursor,
        "examined": now.examined,
        "finished": now.finished,
        "updated_at": before.updated_at.isoformat(sep=" ", timespec="seconds"),
    }


def handle_backfill_article_files(req: BackfillArticleFilesRequest) -> Reply:
    """Phase 2.2 chunked backfill of `article_files`."""
    from mimir.patches import backfill_article_files

    return _run_backfill_chunk(
        req, "backfill_article_files", backfill_article_files, reprocess=req.reprocess
    )


def handle_backfill_article_trailers(req: BackfillArticleTrailersRequest) -> Reply:
    """Phase 2.2 chunked backfill of `article_trailers`."""
    from mimir.trailers import backfill_article_trailers

    return _run_backfill_chunk(
        req,
        "backfill_article_trailers",
        backfill_article_trailers,
        reprocess=req.reprocess,
    )


def handle_backfill_patch_series(req: BackfillPatchSeriesRequest) -> Reply:
    """Phase 2.2 chunked backfill of `patch_series_*` columns on
    articles. Cheaper per-row than the body-re-reading backfills
    (subject+author only); still benefits from cooperative
    scheduling on `--reprocess` runs over the full corpus."""
    from mimir.patch_series import backfill_patch_series

    return _run_backfill_chunk(
        req, "backfill_patch_series", backfill_patch_series, reprocess=req.reprocess
    )


def handle_backfill_series_chains(req: BackfillSeriesChainsRequest) -> Reply:
    """Chunked backfill of `patch_series_head_id` (#816): splits keys
    that hold unrelated postings under one cover title."""
    from mimir.patch_series_chains import backfill_series_chains

    return _run_backfill_chunk(req, "backfill_series_chains", backfill_series_chains)


def handle_backfill_canonicals(req: BackfillCanonicalsRequest) -> Reply:
    """Phase 2.2 chunked backfill of `articles.canonical_inbox_id`
    via re-reading To/Cc headers. Heaviest of the four (blob fetch
    per article) and the one that bit operators on 2026-05-18; the
    cooperative-scheduling shape here exists primarily to keep
    `--reprocess` runs from blocking ingest ticks."""
    from mimir.ingest.backfill import backfill_canonicals

    return _run_backfill_chunk(
        req,
        "backfill_canonicals",
        backfill_canonicals,
        reprocess=req.reprocess,
        inbox_filter=req.inbox_filter,
    )
