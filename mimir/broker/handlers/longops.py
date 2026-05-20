"""Long-op handlers. Each is a multi-second to multi-minute
operation that holds the SQLite writer lock for a non-trivial
duration: full-inbox ingest (Phase 2.1), backfills (Phase 2.2),
update-mainline (Phase 2.3), analyze, vacuum (Phase 2.3). These
route to the broker's **long worker** (the long_queue) so they
don't block cache writes serving the web tier in the meantime;
the two workers contend for the SQLite writer lock at the SQLite
level, but cache writes only wait for the long op's current
commit batch, not the whole op.

Imports for `mimir.ingest.*`, `mimir.models`, etc. are deferred
into each handler body so the broker process's import-time graph
stays lean. Heavy imports only land when the first long-op RPC
actually arrives (which is typically much later than process
start, since the broker boots fast and long ops fire on the
scheduler's tick schedule).
"""
from mimir.broker.protocol import (
    BootstrapInboxesRequest,
    IngestInboxRequest,
    Reply,
)
from mimir.extensions import write_transaction


def handle_bootstrap_inboxes(req: BootstrapInboxesRequest) -> Reply:
    """Reconcile `Settings.inboxes` env config into the `inboxes`
    table. Delegates to `mimir.inboxes.bootstrap_inboxes()` (the
    same function the scheduler-tasks container called directly
    pre-Phase-2)."""
    from mimir.inboxes import bootstrap_inboxes
    with write_transaction("broker:bootstrap_inboxes"):
        inboxes = bootstrap_inboxes()
    return Reply(ok=True, result={"inboxes": len(inboxes)})


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
            ok=False, error=f"UnknownInbox:{req.inbox_name}",
        )
    workers = req.workers if req.workers is not None else DEFAULT_WORKERS
    results = ingest_inbox(inbox, limit=req.limit, workers=workers)
    return Reply(
        ok=True,
        result={"results": [r.model_dump(mode="json") for r in results]},
    )
