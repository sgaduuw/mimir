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
- `handlers`: RPC dispatch + per-op handlers. Each handler calls
  `mimir.cache._direct_<op>(...)` against a pooled writer
  connection wrapped in `write_transaction()`.
- `client`: connection lifecycle + RPC API on the calling side.
"""
from mimir.broker.client import BrokerClient, BrokerUnavailable, get_broker_client
from mimir.broker.server import serve

__all__ = [
    "BrokerClient",
    "BrokerUnavailable",
    "get_broker_client",
    "serve",
]
