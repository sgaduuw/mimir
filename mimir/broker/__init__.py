"""Write-broker package: a long-running daemon owns the cache-table
writer connection; client processes (gunicorn workers, scheduler
sidecar CLI invocations) submit cache write intents over a UNIX
domain socket. Eliminates SQLite writer-lock contention between
gunicorn and the scheduler, which was the load-bearing source of
silently-dropped `cache.set` writes and front-page stalls.

Public surface re-exported here so callers import from the
package, not the submodules:

- `BrokerClient`, `BrokerUnavailable`: from `client`. The client is
  process-singleton; the cache module uses `get_broker_client()` to
  forward writes.
- `serve`: from `server`. Entry point used by `mimir broker`.

The submodules split by concern:

- `protocol`: wire message shapes (pydantic). JSONL over UNIX
  socket; one request per line, one reply per line.
- `server`: daemon lifecycle. `socketserver.UnixStreamServer`
  subclass, accept loop, signal handler for clean shutdown.
- `handlers`: RPC dispatch + per-op handlers. Reads run on a
  `ReadSessionPool` session; writes dispatch through the active
  `WriterThread` (two-pool restructure, complete as of Phase 6).
- `client`: connection lifecycle + RPC API on the calling side.
"""

from mimir.broker.client import BrokerClient, BrokerUnavailable, get_broker_client
from mimir.broker.server import serve

# Two-pool restructure primitives
# (_claude/specs/2026-05-29-broker-two-pool-design.md): the read pool
# and the single writer thread the broker's handlers run reads and
# writes through. Re-exported on the package surface per the house
# "re-export the public surface" convention.
from mimir.broker.pools import ReadSessionPool
from mimir.broker.writes import WriteOp, WriterThread

__all__ = [
    "BrokerClient",
    "BrokerUnavailable",
    "get_broker_client",
    "serve",
    "ReadSessionPool",
    "WriteOp",
    "WriterThread",
]
