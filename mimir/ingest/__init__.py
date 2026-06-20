"""Ingest pipeline split across four submodules:

- `epoch`: the hot per-epoch walk (`ingest_epoch`), the `IngestResult`
  model, and the shared helper (`_to_article`) that `replay` / `backfill`
  / `orchestrate` reuse. The observation-flush and list-address-promotion
  writes live as composite WriteOps in `._pending`. The tz-coercion
  helper (`aware_utc`) moved out of this package into
  `mimir.datetime_utils` once a non-ingest caller (`patch_state.py`)
  needed the same shape.
- `replay`: re-walking persisted `parse_failures` rows after a parser fix
  (`replay_failures` + `ReplayResult`).
- `backfill`: historical re-walk to populate `canonical_inbox_id`
  (`backfill_canonicals` + `BackfillResult`).
- `orchestrate`: per-inbox and across-all-inboxes drivers
  (`discover_epochs`, `ingest_inbox`, `ingest_all`).
"""

from mimir.ingest.backfill import BackfillResult, backfill_canonicals
from mimir.ingest.epoch import (
    COMMIT_EVERY,
    DEFAULT_WORKERS,
    MIN_PROMOTE_OBSERVATIONS,
    PARSE_CHUNKSIZE,
    PROGRESS_EVERY,
    PROMOTE_DOMINANCE,
    IngestResult,
    ingest_epoch,
)
from mimir.ingest.orchestrate import (
    discover_epochs,
    ingest_all,
    ingest_inbox,
)
from mimir.ingest.replay import ReplayResult, replay_failures

__all__ = [
    "COMMIT_EVERY",
    "DEFAULT_WORKERS",
    "MIN_PROMOTE_OBSERVATIONS",
    "PARSE_CHUNKSIZE",
    "PROGRESS_EVERY",
    "PROMOTE_DOMINANCE",
    "BackfillResult",
    "IngestResult",
    "ReplayResult",
    "backfill_canonicals",
    "discover_epochs",
    "ingest_all",
    "ingest_epoch",
    "ingest_inbox",
    "replay_failures",
]
