"""Helpers shared by multiple CLI submodules.

`_configure_logging` and `_select_inboxes` are invoked from every command
that takes a verbosity flag or an `--inbox` filter; `_fmt_bytes` formats
sizes for the maintenance commands; `_parse_pair` parses the
`LABEL=SUBSTRING` shape used by `admin inbox trackers set`.
"""

import logging
import re

import click

from mimir.inboxes import bootstrap_inboxes
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
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)


def _select_inboxes(only: str | None) -> dict[str, Inbox]:
    """Bootstrap from env, then narrow to one inbox name if provided."""
    inboxes = bootstrap_inboxes()
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


def _broker_backfill_loop(
    call,
    *,
    limit: int | None,
    reprocess: bool,
    extra: dict | None = None,
):
    """Run a chunked backfill RPC loop against the broker, summing
    per-chunk counters into one `BackfillResult`.

    `call` is a bound `BrokerClient.backfill_*` method. Each RPC
    advances by at most one chunk (the broker's
    `broker_backfill_chunk_seconds` budget); the loop continues while
    `partial=True` is returned, supplying `continuation=` so the
    handler resumes at `Article.id < continuation`. `extra` carries
    per-backfill extras (e.g. `inbox_filter` for canonicals).

    Cross-chunk `--limit` semantics: each iteration subtracts the
    chunk's `examined` from the remaining cap and stops once it
    reaches zero. The per-RPC `limit` is the remaining budget so the
    handler short-circuits at the right total.
    """
    extra = extra or {}
    aggregated = None
    remaining = limit
    continuation = None
    while True:
        chunk = call(
            limit=remaining,
            reprocess=reprocess,
            continuation=continuation,
            **extra,
        )
        aggregated = chunk if aggregated is None else aggregated.merge(chunk)
        if not chunk.partial:
            return aggregated
        if remaining is not None:
            remaining -= chunk.examined
            if remaining <= 0:
                return aggregated
        continuation = chunk.continuation
