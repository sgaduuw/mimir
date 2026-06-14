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
committing what it processed and returning `partial=True,
continuation=<last id>` to the caller. The CLI loops with a follow-
up RPC, and between two chunks the long worker is free to service
queued cache writes / other long ops. Without this, a multi-hour
`backfill_canonicals --reprocess` would block every queued
scheduler `update` tick behind it.

Imports for `mimir.ingest.*`, `mimir.models`, etc. are deferred
into each handler body so the broker process's import-time graph
stays lean. Heavy imports only land when the first long-op RPC
actually arrives (which is typically much later than process
start, since the broker boots fast and long ops fire on the
scheduler's tick schedule).
"""

from mimir.broker.protocol import (
    BackfillArticleFilesRequest,
    BackfillArticleTrailersRequest,
    BackfillCanonicalsRequest,
    BackfillPatchSeriesRequest,
    BootstrapInboxesRequest,
    IngestInboxRequest,
    Reply,
)


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


def _chunk_seconds() -> float:
    """Per-chunk time budget for the backfill handlers. Read from
    settings on each call so an env override picks up without
    restarting the broker (settings is a singleton but it's safe to
    re-read attrs; the value is an int)."""
    from mimir.config import settings

    return float(settings.broker_backfill_chunk_seconds)


def _backfill_reply(req, result) -> Reply:
    """Shared shape for the four backfill handlers' Reply: the
    `BackfillResult` model is serialised whole as `counters`, and
    `partial`/`continuation` are mirrored out so the CLI can branch
    on them without re-parsing the counters dict. The CLI also
    reconstructs the typed `BackfillResult` from `counters` to feed
    its `merge()` chain."""
    return Reply(
        rpc_id=req.rpc_id,
        ok=True,
        result={
            "counters": result.model_dump(mode="json"),
            "partial": bool(result.partial),
            "continuation": result.continuation,
        },
    )


def handle_backfill_article_files(req: BackfillArticleFilesRequest) -> Reply:
    """Phase 2.2 chunked backfill of `article_files`. Delegates to
    `mimir.patches.backfill_article_files` with the broker's time
    budget; that helper wraps its work in
    `write_transaction("backfill_article_files")` already."""
    from mimir.patches import backfill_article_files

    result = backfill_article_files(
        limit=req.limit,
        reprocess=req.reprocess,
        max_seconds=_chunk_seconds(),
        start_cursor=req.continuation,
    )
    return _backfill_reply(req, result)


def handle_backfill_article_trailers(req: BackfillArticleTrailersRequest) -> Reply:
    """Phase 2.2 chunked backfill of `article_trailers`."""
    from mimir.trailers import backfill_article_trailers

    result = backfill_article_trailers(
        limit=req.limit,
        reprocess=req.reprocess,
        max_seconds=_chunk_seconds(),
        start_cursor=req.continuation,
    )
    return _backfill_reply(req, result)


def handle_backfill_patch_series(req: BackfillPatchSeriesRequest) -> Reply:
    """Phase 2.2 chunked backfill of `patch_series_*` columns on
    articles. Cheaper per-row than the body-re-reading backfills
    (subject+author only); still benefits from cooperative
    scheduling on `--reprocess` runs over the full corpus."""
    from mimir.patch_series import backfill_patch_series

    result = backfill_patch_series(
        limit=req.limit,
        reprocess=req.reprocess,
        max_seconds=_chunk_seconds(),
        start_cursor=req.continuation,
    )
    return _backfill_reply(req, result)


def handle_backfill_canonicals(req: BackfillCanonicalsRequest) -> Reply:
    """Phase 2.2 chunked backfill of `articles.canonical_inbox_id`
    via re-reading To/Cc headers. Heaviest of the four (blob fetch
    per article) and the one that bit operators on 2026-05-18; the
    cooperative-scheduling shape here exists primarily to keep
    `--reprocess` runs from blocking ingest ticks."""
    from mimir.ingest.backfill import backfill_canonicals

    result = backfill_canonicals(
        inbox_filter=req.inbox_filter,
        limit=req.limit,
        reprocess=req.reprocess,
        max_seconds=_chunk_seconds(),
        start_cursor=req.continuation,
    )
    return _backfill_reply(req, result)
