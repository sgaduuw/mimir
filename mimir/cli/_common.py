"""Helpers shared by multiple CLI submodules.

`_configure_logging` and `_select_inboxes` are invoked from every command
that takes a verbosity flag or an `--inbox` filter; `_fmt_bytes` formats
sizes for the maintenance commands; `_parse_pair` parses the
`LABEL=SUBSTRING` shape used by `admin inbox trackers set`.
"""

import logging
import re
import shlex
import sys
import time

import click

from mimir.inboxes import list_inboxes, pinned_first
from mimir.models import Inbox

# Public-inbox epoch directory names are always `<N>.git` for
# integer N. Operators pass this string into `reindex` and we then
# join it onto `inbox.mirror_path`; without a shape check, a value
# like `../../etc` would walk outside the mirror root before the
# `.exists()` guard ever runs.
_EPOCH_RE = re.compile(r"\d+\.git")

# A backfill chunk that meets the weekly VACUUM gets `database is
# locked` after SQLITE_BUSY_TIMEOUT_MS and the broker replies
# `OperationalError`. The one VACUUM measured took 198 s on a ~16 GB
# database (production, 2026-10-25), so 60 waits of 30 s (30 min) is
# about 9x that, room for the archive to grow. A cap that runs out
# costs what a lock cost before #885: the run stops and is resumed by
# hand.
_LOCKED_RETRY_SEC = 30
_LOCKED_RETRIES = 60


def _configure_logging(verbose: int) -> None:
    """Install the CLI's stream handler on first call, then set level.

    `logging.basicConfig` is a no-op once the root logger already has
    a handler. Repeated CLI invocations sharing a Python process
    (tests, library mode, the test runner itself) silently kept
    whatever level the first call set, so `-vv` after a quiet call
    never raised verbosity. Setting the level directly on the root
    logger takes effect every time; we still install a stream
    handler on first run so the format is consistent. Audit
    (2026-05-15) flagged the silent re-config break.
    """
    if verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        # `%(asctime)s` is load-bearing on the container tiers, not
        # decoration. The broker's startup does several multi-minute
        # steps back to back (migration, bootstrap, ANALYZE, thread-root
        # backfill), and without a timestamp per line an operator
        # watching a deploy cannot tell which step is running, which one
        # is slow, or whether anything is progressing at all.
        #
        # `podman logs --timestamps` is NOT a substitute: it stamps when
        # the runtime CAPTURED the line, which for buffered stderr is
        # not when the event happened, and the skew is worst across
        # exactly the long steps you are trying to time. It is also
        # opt-in, so the default `podman logs` view has nothing.
        #
        # This handler is what timestamps the BROKER's migration output
        # too, which is not obvious: `_migrate_if_needed` builds its
        # alembic `Config()` programmatically and deliberately never
        # loads `alembic.ini` (loading it would run `fileConfig` and
        # clear every root handler). So alembic's own
        # `formatter_generic` is not installed in the broker container,
        # and alembic's `Running upgrade ...` lines are formatted by
        # THIS handler, via the root logger. `alembic.ini`'s formatter
        # governs only direct `alembic` CLI invocations.
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)
    root.setLevel(level)


def _select_inboxes(only: str | None) -> dict[str, Inbox]:
    """Read the inbox table, then narrow to one inbox name if provided.

    The broker self-bootstraps `inboxes` on its startup (post-2.0.0
    every other process opens connections with `PRAGMA query_only=1`),
    so any CLI invocation in `mimir-tasks` / `mimir` containers MUST
    read here rather than re-running the bootstrap insert. The pre-
    2.0.0 shape called `bootstrap_inboxes()` defensively; against a
    read-only connection that raises `OperationalError: attempt to
    write a readonly database` and the whole `update`/`ingest` tick
    aborts before any inbox gets fetched.
    """
    # Pinned first (#766); see CONTEXT.md "Canonical-inbox resolution".
    inboxes = {inbox.name: inbox for inbox in pinned_first(list_inboxes())}
    if only is None:
        return inboxes
    if only not in inboxes:
        raise click.ClickException(f"unknown inbox: {only!r}")
    return {only: inboxes[only]}


def _fmt_bytes(n: int) -> str:
    """Human-readable byte size for the vacuum / status output."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _parse_pair(pair: str) -> tuple[str, str]:
    if "=" not in pair:
        raise click.ClickException(f"expected LABEL=SUBSTRING, got {pair!r}")
    label, substring = pair.split("=", 1)
    return label, substring


def _verbose_broker_hint(verbose: int) -> None:
    """A `-v` flag asks for per-batch progress that flows via the
    broker's own log rather than the CLI's stdout. Tell the operator
    once where to look instead of silently no-op'ing the flag."""
    if verbose:
        click.echo(
            "per-batch progress flows via broker logs "
            "(e.g. `podman logs -f mimir-broker`)",
            err=True,
        )


def backfill_options(*, reprocess_help: str | None):
    """The options every backfill command shares, in `--help` order.
    `reprocess_help=None` leaves `--reprocess` out, for a backfill that
    always recomputes whole units."""
    options = [
        click.option(
            "--limit",
            type=int,
            default=None,
            help="Cap the number of articles examined this session.",
        ),
        *(
            [click.option("--reprocess", is_flag=True, help=reprocess_help)]
            if reprocess_help is not None
            else []
        ),
        click.option(
            "--restart",
            is_flag=True,
            help="Discard an unfinished run of this backfill and start again "
            "from the newest article. A run that has examined articles is "
            "kept unless --yes is also given. Without --restart, an "
            "interrupted run resumes.",
        ),
        click.option(
            "--yes",
            is_flag=True,
            help="With --restart, discard an unfinished run that has progress "
            "instead of refusing.",
        ),
        click.option(
            "-v",
            "--verbose",
            count=True,
            help="-v: progress hint (broker mode logs to the broker process; "
            "use `podman logs -f mimir-broker` to follow).",
        ),
    ]

    def decorate(func):
        # Click lists options in decorator order, which is bottom-up
        # application, so apply the last one first.
        for option in reversed(options):
            func = option(func)
        return func

    return decorate


def _broker_backfill_loop(
    client,
    request_cls,
    result_cls,
    *,
    limit: int | None,
    reprocess: bool | None = None,
    restart: bool = False,
    yes: bool = False,
    extra: dict | None = None,
):
    """Run a chunked backfill RPC loop against the broker, summing
    per-chunk counters into one `BackfillResult`. Returns
    `(result, run)` where `run` is the last chunk's run info.

    Each chunk is one `client.call(request_cls(...))`, its counters
    parsed into `result_cls`. Each RPC
    advances by at most one chunk (the broker's
    `broker_backfill_chunk_seconds` budget); the loop continues while
    `partial=True` is returned. The broker keeps the run's cursor, so
    every chunk, and the next invocation after an interruption,
    continues where the last committed batch stopped. Only the first
    RPC carries `start`, so only it may start a run: "restart" when
    `restart` is set, else "resume". A restart discards an unfinished
    run only if it has no progress or `yes` is set ("force_restart");
    otherwise the broker refuses and this raises, naming the run and
    the command that resumes it. A discarded run is reported first.
    Whatever the start, a run another session is driving (see
    `mimir.backfill_runs`) is refused and this raises.
    `extra` carries per-backfill extras (e.g. `inbox_filter` for
    canonicals). `reprocess=None` sends no `reprocess`, for a request
    that has none.

    Cross-chunk `--limit` semantics: each iteration subtracts the
    chunk's `examined` from the remaining cap and stops once it
    reaches zero. The per-RPC `limit` is the remaining budget so the
    handler short-circuits at the right total. The cap is per session:
    a run stopped by it stays unfinished and the next session resumes.

    An `OperationalError` reply to a later chunk (the database locked,
    usually by the weekly VACUUM) resends the same chunk every
    `_LOCKED_RETRY_SEC`, at most `_LOCKED_RETRIES` times in a row,
    then raises. Resending is safe because a failed batch rolls back
    with its cursor, and `start=None` joins the run without the busy
    check. A first chunk is not retried: its run lookup may have
    committed the claim before the lock hit, and a resend with `start`
    is then refused as busy by that claim. Any other error raises at
    once.
    """
    from mimir.broker.client import BrokerUnavailable

    extra = extra or {}
    if reprocess is not None:
        extra = {**extra, "reprocess": reprocess}
    aggregated = None
    remaining = limit
    start = ("force_restart" if yes else "restart") if restart else "resume"
    while True:
        req = request_cls(limit=remaining, start=start, **extra)
        for attempt in range(_LOCKED_RETRIES + 1):
            try:
                reply = client.call(req)
                break
            except BrokerUnavailable as exc:
                # The exact text `BrokerClient.call` builds for the
                # reply; a socket failure or a crash reads differently.
                locked = str(exc) == f"{req.op}: OperationalError"
                if not locked or start is not None or attempt == _LOCKED_RETRIES:
                    raise
                if attempt == 0:
                    click.echo(
                        f"{req.op}: broker reported OperationalError (often "
                        "the database locked by VACUUM; the broker log has "
                        "the cause); retrying the chunk every "
                        f"{_LOCKED_RETRY_SEC} s for up to "
                        f"{_LOCKED_RETRY_SEC * _LOCKED_RETRIES // 60} min",
                        err=True,
                    )
                time.sleep(_LOCKED_RETRY_SEC)
        chunk = result_cls.model_validate(reply.get("counters", {}))
        run = reply.get("run")
        start = None
        if run is not None and run.get("busy"):
            at = f", at id {run['cursor']}" if run["cursor"] is not None else ""
            raise click.ClickException(
                f"run {run['id']} is in progress (last chunk "
                f"{run['updated_at']} UTC{at}); another session is driving "
                f"it. Stop it first, or wait {run['retry_after']} s if it "
                "was just killed."
            )
        if run is not None and run.get("refused"):
            _echo_old_run("refusing to discard", run)
            click.echo(
                "resume it with: "
                + shlex.join(a for a in sys.argv if a != "--restart"),
                err=True,
            )
            raise click.ClickException(
                "the unfinished run has progress; resume it, or add --yes to discard it"
            )
        if aggregated is None:
            aggregated = chunk
            if run is not None and "discarded" in run:
                _echo_old_run("discarding", run["discarded"])
            if run["cursor"] is not None:
                click.echo(
                    f"resuming run started {run['started_at']} UTC "
                    f"at id {run['cursor']}",
                    err=True,
                )
        else:
            aggregated = aggregated.merge(chunk)
        if not chunk.partial:
            return aggregated, run
        if remaining is not None:
            remaining -= chunk.examined
            if remaining <= 0:
                return aggregated, run


def _echo_old_run(verb: str, run: dict) -> None:
    """Say which run a `--restart` is discarding, or refusing to."""
    # A run that has not finished a batch has no cursor yet.
    at = f" at id {run['cursor']}" if run["cursor"] is not None else ""
    click.echo(
        f"{verb} run {run['id']} started {run['started_at']} UTC{at} "
        f"({run['examined']} examined)",
        err=True,
    )


def _echo_run_summary(run: dict | None) -> None:
    """The line after a backfill's per-session counters: the counters
    cover this session only, so say which run they belong to, how far
    that run has got across every session, and whether it is finished
    or still open for the next session to resume. A run deleted under
    this session reports its snapshot from before the chunk. `run` is
    None only if a follow-up chunk found no run at all for the key."""
    if run is None:
        return
    state = "finished" if run["finished"] else "open"
    click.echo(
        f"run started {run['started_at']} UTC: "
        f"{run['examined']} examined in total, {state}"
    )
