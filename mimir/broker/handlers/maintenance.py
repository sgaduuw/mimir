"""Maintenance-op handlers (Phase 2.3) + admin-write handlers
(Phase 2.4): the periodic SQLite hygiene + mainline-tree refresh
ops, plus the admin-CRUD ops that the operator runs interactively.

Sibling to `longops.py` (ingest, backfills, bootstrap) and
`warm.py` (cache warming). Split off into its own module so the
"periodic + admin writers" concern stays narrow as the long-op
family keeps growing; the dispatch table in
`handlers/__init__.py` re-exports the handlers uniformly.

Every handler delegates to a public function in `mimir.mainline`
/ `mimir.maintenance` / `mimir.inboxes` / `mimir.ingest` rather
than re-implementing the body. The CLI command stays a thin click
wrapper around the same function, and the broker handler's only
job is to translate the RPC into a Python call.

VACUUM is the load-bearing exception: it acquires the SQLite
exclusive lock for the duration of the operation, which freezes
every other broker worker (cache + long + warm). The handler
emits a high-visibility WARNING at start so an operator
correlating a cache-write stall against the broker log can tell
"weekly maintenance, not a fault." Documented in CONTEXT.md.

Admin-CRUD error mapping: the `InboxNotFound` and
`InboxValidationError` raised by the service layer turn into
`Reply(ok=False, error="InboxNotFound:<name>")` /
`Reply(ok=False, error="InvalidInbox:<msg>")` so the CLI shim can
re-raise as `ClickException` with the operator-facing text intact.
"""

import logging

from mimir.broker.protocol import (
    AnalyzeRequest,
    FailuresReplayRequest,
    InboxAddTrackedAuthorRequest,
    InboxClearTrackedAuthorsRequest,
    InboxCreateRequest,
    InboxDeleteRequest,
    InboxRemoveTrackedAuthorRequest,
    InboxSetTrackedAuthorsRequest,
    InboxUpdateRequest,
    Reply,
    RobotsAddRequest,
    RobotsRemoveRequest,
    RobotsResetRequest,
    RobotsUpdateRequest,
    UpdateMainlineRequest,
    VacuumRequest,
)

logger = logging.getLogger(__name__)


def handle_update_mainline(req: UpdateMainlineRequest) -> Reply:
    """Run the full `update_mainline` flow inside the broker process.
    Returns the structured result so the CLI can echo the same
    "loaded N subsystems" / "walked N commits" lines.

    Per-tree exceptions (including FileNotFoundError from a missing
    MAINTAINERS file) are caught inside `update_mainline()` and
    recorded as `result.trees[slug].error`; they do not propagate
    here."""
    from mimir.mainline import update_mainline

    result = update_mainline(
        skip_fetch=req.skip_fetch,
        skip_maintainers=req.skip_maintainers,
        skip_commits=req.skip_commits,
        force=req.force,
    )
    return Reply(rpc_id=req.rpc_id, ok=True, result=result.model_dump(mode="json"))


def handle_analyze(req: AnalyzeRequest) -> Reply:
    """Run ANALYZE on the broker. `full=True` triggers the no-cap
    pass (the weekly safety-net); default is the cheap bounded
    pass governed by `Settings.analyze_limit`."""
    from mimir.maintenance import run_analyze

    result = run_analyze(full=req.full)
    return Reply(rpc_id=req.rpc_id, ok=True, result=result.model_dump(mode="json"))


def handle_vacuum(req: VacuumRequest) -> Reply:
    """Run VACUUM on the broker.

    Holds the SQLite exclusive lock for the duration; every other
    broker worker pauses, and cache writes from the web tier queue
    behind the lock until either the VACUUM completes or the
    client's per-RPC timeout expires (matching today's direct-CLI
    VACUUM contract).

    The WARNING is the operator-visible "this is the weekly
    maintenance window" signal; without it, an operator who sees
    cache.set RPC timeouts during the window has to cross-correlate
    the cron schedule manually."""
    from mimir.maintenance import run_vacuum

    logger.warning(
        "broker: pausing for VACUUM; cache writes may time out for "
        "the duration of the operation",
    )
    result = run_vacuum()
    logger.info(
        "broker: VACUUM finished in %d ms, reclaimed %d bytes",
        result.elapsed_ms,
        result.reclaimed,
    )
    return Reply(rpc_id=req.rpc_id, ok=True, result=result.model_dump(mode="json"))


# ----- Phase 2.4: admin-write ops ----------------------------------------


def _inbox_error_reply(req, exc: Exception) -> Reply:
    """Map an admin-CRUD service-layer exception to a structured
    failure Reply. The CLI shim picks the prefix apart and re-raises
    as ClickException so operator-facing text stays intact."""
    from mimir.inboxes import InboxNotFound, InboxValidationError

    if isinstance(exc, InboxNotFound):
        return Reply(rpc_id=req.rpc_id, ok=False, error=f"InboxNotFound:{exc}")
    if isinstance(exc, InboxValidationError):
        return Reply(rpc_id=req.rpc_id, ok=False, error=f"InvalidInbox:{exc}")
    raise exc  # Anything else propagates to the dispatch layer.


def _inbox_to_dict(inbox) -> dict:
    """Project an `Inbox` ORM row to the dict shape the admin client
    methods reconstruct. Detached at call time (service-layer
    functions all `session.expunge` before returning), so attribute
    reads are safe outside a session."""
    return {
        "id": inbox.id,
        "name": inbox.name,
        "mirror_path": inbox.mirror_path,
        "upstream_url": inbox.upstream_url,
        "tracked_authors": dict(inbox.tracked_authors or {}),
    }


def handle_inbox_create(req: InboxCreateRequest) -> Reply:
    """Insert one inbox via `mimir.inboxes.create_inbox`."""
    from mimir.inboxes import (
        InboxNotFound,
        InboxValidationError,
        create_inbox,
    )

    try:
        inbox = create_inbox(
            req.name,
            mirror_path=req.mirror_path,
            upstream_url=req.upstream_url,
        )
    except (InboxNotFound, InboxValidationError) as exc:
        return _inbox_error_reply(req, exc)
    return Reply(rpc_id=req.rpc_id, ok=True, result={"inbox": _inbox_to_dict(inbox)})


def handle_inbox_update(req: InboxUpdateRequest) -> Reply:
    """Modify one inbox via `mimir.inboxes.update_inbox`. Only the
    request's non-None fields land."""
    from mimir.inboxes import (
        InboxNotFound,
        InboxValidationError,
        update_inbox,
    )

    try:
        inbox = update_inbox(
            req.name,
            new_name=req.new_name,
            mirror_path=req.mirror_path,
            upstream_url=req.upstream_url,
        )
    except (InboxNotFound, InboxValidationError) as exc:
        return _inbox_error_reply(req, exc)
    return Reply(rpc_id=req.rpc_id, ok=True, result={"inbox": _inbox_to_dict(inbox)})


def handle_inbox_delete(req: InboxDeleteRequest) -> Reply:
    """Remove one inbox via `mimir.inboxes.delete_inbox`. CLI is
    responsible for the operator confirmation prompt; this handler
    only runs the post-confirmation work."""
    from mimir.inboxes import InboxNotFound, delete_inbox

    try:
        report = delete_inbox(
            req.name,
            keep_orphan_articles=req.keep_orphan_articles,
            remove_inbox_data=req.remove_inbox_data,
        )
    except InboxNotFound as exc:
        return _inbox_error_reply(req, exc)
    return Reply(
        rpc_id=req.rpc_id,
        ok=True,
        result={
            "report": {
                "name": report.name,
                "article_lists_deleted": report.article_lists_deleted,
                "ingest_state_deleted": report.ingest_state_deleted,
                "orphan_articles_deleted": report.orphan_articles_deleted,
                "mirror_path_deleted": report.mirror_path_deleted,
            }
        },
    )


def handle_inbox_set_tracked_authors(
    req: InboxSetTrackedAuthorsRequest,
) -> Reply:
    """Replace the per-inbox tracker dict in one shot."""
    from mimir.inboxes import (
        InboxNotFound,
        InboxValidationError,
        set_tracked_authors,
    )

    try:
        inbox = set_tracked_authors(req.name, req.trackers)
    except (InboxNotFound, InboxValidationError) as exc:
        return _inbox_error_reply(req, exc)
    return Reply(rpc_id=req.rpc_id, ok=True, result={"inbox": _inbox_to_dict(inbox)})


def handle_inbox_add_tracked_author(
    req: InboxAddTrackedAuthorRequest,
) -> Reply:
    """Add (or replace) one tracker entry."""
    from mimir.inboxes import (
        InboxNotFound,
        InboxValidationError,
        add_tracked_author,
    )

    try:
        inbox = add_tracked_author(req.name, req.label, req.substring)
    except (InboxNotFound, InboxValidationError) as exc:
        return _inbox_error_reply(req, exc)
    return Reply(rpc_id=req.rpc_id, ok=True, result={"inbox": _inbox_to_dict(inbox)})


def handle_inbox_remove_tracked_author(
    req: InboxRemoveTrackedAuthorRequest,
) -> Reply:
    """Remove one tracker entry by label."""
    from mimir.inboxes import (
        InboxNotFound,
        InboxValidationError,
        remove_tracked_author,
    )

    try:
        inbox = remove_tracked_author(req.name, req.label)
    except (InboxNotFound, InboxValidationError) as exc:
        return _inbox_error_reply(req, exc)
    return Reply(rpc_id=req.rpc_id, ok=True, result={"inbox": _inbox_to_dict(inbox)})


def handle_inbox_clear_tracked_authors(
    req: InboxClearTrackedAuthorsRequest,
) -> Reply:
    """Drop all tracker entries (writes NULL)."""
    from mimir.inboxes import InboxNotFound, clear_tracked_authors

    try:
        inbox = clear_tracked_authors(req.name)
    except InboxNotFound as exc:
        return _inbox_error_reply(req, exc)
    return Reply(rpc_id=req.rpc_id, ok=True, result={"inbox": _inbox_to_dict(inbox)})


# ----- Phase 2.4: admin failures replay ----------------------------------


def handle_failures_replay(req: FailuresReplayRequest) -> Reply:
    """Re-parse persisted parse_failures for one inbox. Delegates to
    `replay_failures` so the broker path runs the same code as
    direct. Looks the Inbox row up server-side from the request's
    `inbox_name`."""
    from mimir.inboxes import InboxNotFound, get_inbox
    from mimir.ingest.replay import replay_failures

    try:
        inbox = get_inbox(req.inbox_name)
    except InboxNotFound as exc:
        return _inbox_error_reply(req, exc)
    result = replay_failures(
        inbox,
        epoch_filter=req.epoch_filter,
        limit=req.limit,
    )
    return Reply(rpc_id=req.rpc_id, ok=True, result=result.model_dump(mode="json"))


# ----- robots admin ------------------------------------------------------


def _robots_error_reply(req, exc: Exception) -> Reply:
    """Map a robots service-layer exception to a structured failure
    Reply. The CLI shim picks the prefix apart and re-raises as
    ClickException."""
    from mimir.robots import RobotsRuleNotFound, RobotsValidationError

    if isinstance(exc, RobotsRuleNotFound):
        return Reply(rpc_id=req.rpc_id, ok=False, error=f"RobotsRuleNotFound:{exc}")
    if isinstance(exc, RobotsValidationError):
        return Reply(rpc_id=req.rpc_id, ok=False, error=f"InvalidRobotsRule:{exc}")
    raise exc


def _robots_rule_to_dict(rule) -> dict:
    """Project a `RobotsRule` ORM row to the dict shape the admin
    client methods reconstruct. Service-layer returns expunge before
    handing back so attribute reads are safe outside a session."""
    return {
        "user_agent": rule.user_agent,
        "crawl_delay": rule.crawl_delay,
        "disallow_paths": list(rule.disallow_paths or []),
        "content_signals": dict(rule.content_signals or {}),
    }


def handle_robots_add(req: RobotsAddRequest) -> Reply:
    from mimir.robots import (
        RobotsRuleNotFound,
        RobotsValidationError,
        add_rule,
    )

    try:
        rule = add_rule(
            req.user_agent,
            disallow=req.disallow,
            crawl_delay=req.crawl_delay,
            content_signals=req.content_signals or None,
        )
    except (RobotsRuleNotFound, RobotsValidationError) as exc:
        return _robots_error_reply(req, exc)
    return Reply(rpc_id=req.rpc_id, ok=True, result={"rule": _robots_rule_to_dict(rule)})


def handle_robots_update(req: RobotsUpdateRequest) -> Reply:
    from mimir.robots import (
        RobotsRuleNotFound,
        RobotsValidationError,
        update_rule,
    )

    try:
        rule = update_rule(
            req.user_agent,
            add_disallow=req.add_disallow,
            remove_disallow=req.remove_disallow,
            crawl_delay=req.crawl_delay,
            clear_crawl_delay=req.clear_crawl_delay,
            set_content_signal=req.set_content_signal or None,
            clear_content_signal=req.clear_content_signal or None,
            clear_all_content_signals=req.clear_all_content_signals,
        )
    except (RobotsRuleNotFound, RobotsValidationError) as exc:
        return _robots_error_reply(req, exc)
    return Reply(rpc_id=req.rpc_id, ok=True, result={"rule": _robots_rule_to_dict(rule)})


def handle_robots_remove(req: RobotsRemoveRequest) -> Reply:
    from mimir.robots import (
        RobotsRuleNotFound,
        RobotsValidationError,
        remove_rule,
    )

    try:
        remove_rule(req.user_agent)
    except (RobotsRuleNotFound, RobotsValidationError) as exc:
        return _robots_error_reply(req, exc)
    return Reply(rpc_id=req.rpc_id, ok=True)


def handle_robots_reset(req: RobotsResetRequest) -> Reply:
    from mimir.robots import reset_rules

    reset_rules()
    return Reply(rpc_id=req.rpc_id, ok=True)


__all__ = [
    "handle_update_mainline",
    "handle_analyze",
    "handle_vacuum",
    "handle_failures_replay",
    "handle_inbox_create",
    "handle_inbox_update",
    "handle_inbox_delete",
    "handle_inbox_set_tracked_authors",
    "handle_inbox_add_tracked_author",
    "handle_inbox_remove_tracked_author",
    "handle_inbox_clear_tracked_authors",
    "handle_robots_add",
    "handle_robots_update",
    "handle_robots_remove",
    "handle_robots_reset",
]
