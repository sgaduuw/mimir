"""Helpers shared by multiple CLI submodules.

`_configure_logging` and `_select_inboxes` are invoked from every command
that takes a verbosity flag or an `--inbox` filter; `_fmt_bytes` formats
sizes for the maintenance commands; `_parse_pair` parses the
`LABEL=SUBSTRING` shape used by `admin inbox trackers set`.
"""

import logging
import re

import click

from mimir.inboxes import list_inboxes
from mimir.models import Inbox

# Public-inbox epoch directory names are always `<N>.git` for
# integer N. Operators pass this string into `reindex` and we then
# join it onto `inbox.mirror_path`; without a shape check, a value
# like `../../etc` would walk outside the mirror root before the
# `.exists()` guard ever runs.
_EPOCH_RE = re.compile(r"\d+\.git")


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
    inboxes = {inbox.name: inbox for inbox in list_inboxes()}
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


def _broker_backfill_loop(
    call,
    *,
    limit: int | None,
    reprocess: bool,
    restart: bool = False,
    extra: dict | None = None,
):
    """Run a chunked backfill RPC loop against the broker, summing
    per-chunk counters into one `BackfillResult`. Returns
    `(result, run)` where `run` is the last chunk's run info.

    `call(**kwargs)` sends one chunk built from those kwargs, usually
    through `BrokerClient.backfill`. Each RPC
    advances by at most one chunk (the broker's
    `broker_backfill_chunk_seconds` budget); the loop continues while
    `partial=True` is returned. The broker keeps the run's cursor, so
    every chunk, and the next invocation after an interruption,
    continues where the last committed batch stopped. Only the first
    RPC carries `start`, so only it may start a run: "restart" when
    `restart` is set, which discards an unfinished run, else "resume".
    `extra` carries per-backfill extras (e.g. `inbox_filter` for
    canonicals).

    Cross-chunk `--limit` semantics: each iteration subtracts the
    chunk's `examined` from the remaining cap and stops once it
    reaches zero. The per-RPC `limit` is the remaining budget so the
    handler short-circuits at the right total. The cap is per session:
    a run stopped by it stays unfinished and the next session resumes.
    """
    extra = extra or {}
    aggregated = None
    remaining = limit
    start = "restart" if restart else "resume"
    while True:
        chunk, run = call(
            limit=remaining,
            reprocess=reprocess,
            start=start,
            **extra,
        )
        start = None
        if aggregated is None:
            aggregated = chunk
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
