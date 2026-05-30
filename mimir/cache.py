"""DB-backed cache for slow dashboard queries.

Cached values are JSON-encoded and stored in the `cache` table, keyed
by string with a unix-second expiry. SQLite's WAL lock makes the
multi-process case (Flask server + warm-cache cron) atomic without
any extra coordination.

Only types registered via `register(tag, cls)` round-trip cleanly.
Each module owning a cached dataclass calls `register` at import time;
the dependency stays one-way (cache knows nothing about its callers).

Every key is silently prefixed with `v{NAMESPACE_VERSION}:` so a code
change that alters cached value shapes (a query rewrite, a renamed
dataclass field) is bumped centrally, old rows simply never match
and age out via `purge_expired`. Callers don't see the prefix.
"""

import dataclasses
import json
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterator

from pydantic import BaseModel
from sqlalchemy import delete as delete_stmt, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import CacheEntry

logger = logging.getLogger(__name__)

# Bump when cached value shapes change (query rewrites, dataclass
# field renames, encoder changes). Old rows fall through to a miss
# and get cleaned up by `purge_expired`.
NAMESPACE_VERSION = 3

# Proactive-refresh window threaded through to `get_or_compute` via a
# context manager. Set by warm-cache so a still-fresh row that's about
# to expire gets recomputed before the next user request sees a cold
# miss, without recomputing rows that are nowhere near expiry. Unset
# (None) means standard cache-aside: only expired rows recompute.
_refresh_within: ContextVar[float | None] = ContextVar("_refresh_within", default=None)


@contextmanager
def refresh_window(seconds: float | None) -> Iterator[None]:
    """Within this scope, `get_or_compute` recomputes any cached row
    whose remaining TTL is less than `seconds`, instead of returning
    the stale-but-not-expired value. Used by warm-cache so 24h-TTL
    keys aren't recomputed on every 5-minute cron tick, only on the
    tick nearest their expiry. The contextvar is snapshotted at
    submission time when callers fan out to a `ThreadPoolExecutor`,
    via `contextvars.copy_context().run(fn, ...)`."""
    token = _refresh_within.set(seconds)
    try:
        yield
    finally:
        _refresh_within.reset(token)


def _ns(key: str) -> str:
    """Apply the cache namespace prefix to a caller-supplied key."""
    return f"v{NAMESPACE_VERSION}:{key}"


# Tag picked explicitly (not `cls.__name__`) so a rename in the source
# can't silently invalidate previously-cached rows or, worse, decode
# them into the wrong type.
_TYPES: dict[str, type] = {}
_TAGS: dict[type, str] = {}


def register(tag: str, cls: type) -> None:
    """Make `cls` round-trip through cache encode/decode under `tag`."""
    _TYPES[tag] = cls
    _TAGS[cls] = tag


def _encode(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, datetime):
        return {"__t": "datetime", "v": obj.isoformat()}
    if isinstance(obj, date):
        return {"__t": "date", "v": obj.isoformat()}
    if isinstance(obj, tuple):
        # Tuples become lists in JSON; recover them on decode via
        # the `tuple` tag for the cases that need it (DailyVolume.days).
        return {"__t": "tuple", "v": [_encode(x) for x in obj]}
    if isinstance(obj, list):
        return [_encode(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    tag = _TAGS.get(type(obj))
    if tag is not None:
        if dataclasses.is_dataclass(obj):
            fields = {
                f.name: _encode(getattr(obj, f.name)) for f in dataclasses.fields(obj)
            }
        elif isinstance(obj, BaseModel):
            # Iterate via `model_fields` + getattr so nested BaseModel /
            # dataclass values recurse through `_encode` (which knows
            # the type tags), instead of through `model_dump()` which
            # would flatten them to plain dicts and lose the tags.
            fields = {
                name: _encode(getattr(obj, name)) for name in type(obj).model_fields
            }
        else:
            raise TypeError(
                f"cache: registered type {type(obj).__name__!r} is neither "
                f"dataclass nor pydantic BaseModel"
            )
        return {"__t": tag, "v": fields}
    raise TypeError(f"cache: don't know how to encode {type(obj).__name__}")


def _decode(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_decode(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    tag = obj.get("__t")
    if tag is None:
        return {k: _decode(v) for k, v in obj.items()}
    v = obj["v"]
    if tag == "datetime":
        return datetime.fromisoformat(v)
    if tag == "date":
        return date.fromisoformat(v)
    if tag == "tuple":
        return tuple(_decode(x) for x in v)
    cls = _TYPES.get(tag)
    if cls is None:
        raise ValueError(f"cache: unknown tag {tag!r}")
    return cls(**{k: _decode(val) for k, val in v.items()})


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _should_dispatch_to_broker() -> bool:
    """True iff this process should send cache writes to the broker
    via RPC. False inside the broker process itself (we'd be RPC'ing
    to ourselves: socket round-trip + queue + dispatch + direct
    write on the cache worker, vs just one direct write here on
    the long worker).

    Two short-circuit conditions for "this is the broker":

    - `MIMIR_IS_BROKER=true` env, which the broker container sets
      in production (and the `mimir broker` CLI entrypoint sets
      when it runs `serve()`). Process-level marker.
    - Thread-local "currently inside a broker handler" flag set by
      `mimir.broker.server`'s worker loops. Catches the in-process
      test shape where broker + CLI live in the same Python
      process and `settings.mimir_is_broker` is shared. Worker
      threads set the flag on entry to a dispatch and clear it on
      exit, so any cache/inboxes call reachable from a handler
      sees False and falls through to the direct path.

    Otherwise the answer is unconditionally True: 2.0.0 made the
    broker the sole writer process, so every non-broker process
    routes writes through it. There's no longer a "broker mode
    off" branch; the dispatch is the contract.
    """
    if settings.mimir_is_broker:
        return False
    if _broker_handler_active():
        return False
    return True


# Thread-local flag set by `mimir.broker.server`'s worker loops
# while they're dispatching a request. `cache._should_dispatch_to_broker`
# checks it (in addition to `settings.mimir_is_broker`) to avoid a
# self-RPC when broker + caller share a process (the in-process
# test shape).
_HANDLER_TLS = threading.local()


def _broker_handler_active() -> bool:
    return getattr(_HANDLER_TLS, "active", False)


def _set_broker_handler_active(active: bool) -> None:
    _HANDLER_TLS.active = active


def get(key: str) -> Any:
    nskey = _ns(key)
    now = _now()
    with SessionLocal() as session:
        row = session.execute(
            select(CacheEntry.value, CacheEntry.expires_at).where(
                CacheEntry.key == nskey
            )
        ).one_or_none()
    if row is None or row.expires_at < now:
        return None
    return _decode(json.loads(row.value))


def get_many(keys: list[str]) -> dict[str, Any]:
    """Bulk read: returns the dict {key: decoded_value} for keys
    present and unexpired. Missing or expired keys are simply
    absent from the result. Single SELECT, used to fan out
    per-row cache lookups across a listing render without N
    round-trips."""
    if not keys:
        return {}
    nskeys = [_ns(k) for k in keys]
    now = _now()
    with SessionLocal() as session:
        rows = session.execute(
            select(CacheEntry.key, CacheEntry.value, CacheEntry.expires_at).where(
                CacheEntry.key.in_(nskeys)
            )
        ).all()
    ns_to_orig = dict(zip(nskeys, keys))
    out: dict[str, Any] = {}
    for nskey, value, expires_at in rows:
        if expires_at < now:
            continue
        out[ns_to_orig[nskey]] = _decode(json.loads(value))
    return out


# Maximum JSON-encoded payload size accepted by `cache.set`. Defense
# in depth against a buggy caller or an over-eager aggregator
# producing huge cache rows; the per-call inputs are bounded by query
# `LIMIT`s today, but the cap closes the door on accidental bloat.
# 8 MiB is well above the largest real cache value observed (~2 MiB
# sitemap XML for popular inboxes) and well below the broker's
# wire-level 16 MiB cap, so a rejection here surfaces with a clean
# log rather than as a broker connection close.
MAX_CACHE_VALUE_BYTES = 8 * 1024 * 1024


def _direct_set(nskey: str, payload: str, ttl: int) -> None:
    """Direct-SQLite upsert into the cache table. Pre-namespaced key,
    pre-encoded value (so this helper is symmetric with the broker's
    handler, which receives the already-namespaced key and JSON
    payload over the wire). Used by `set()` when broker mode is off,
    and imported by `mimir.broker.handlers` so the broker daemon
    runs the same write without recursing into broker mode."""
    expires_at = _now() + ttl
    stmt = (
        sqlite_insert(CacheEntry)
        .values(key=nskey, value=payload, expires_at=expires_at)
        .on_conflict_do_update(
            index_elements=["key"],
            set_={"value": payload, "expires_at": expires_at},
        )
    )
    with SessionLocal() as session:
        session.execute(stmt)
        session.commit()


def set(key: str, value: Any, ttl: int) -> None:
    """Best-effort cache write.

    Forwards the write to the broker daemon via the RPC client.
    `BrokerUnavailable` is logged + swallowed (best-effort
    posture): the page already rendered before we got here, so a
    failed cache write must not propagate, it'd 500 a successful
    response. Next request recomputes.

    Inside the broker process itself the dispatch short-circuits
    via `_should_dispatch_to_broker()` and writes directly via
    `_direct_set`, avoiding a self-RPC.

    Phase 2 of the two-pool restructure
    (`_claude/specs/2026-05-29-broker-two-pool-design.md`):
    when the calling context is inside the broker (the broker's
    serve() loop has registered an active writer), dispatch the
    UPSERT through that writer thread instead of running it
    inline. This keeps the writer-lock hold time short and lets
    multiple read-pool threads' warm work proceed in parallel.

    Outside the broker (no active writer registered; e.g. the
    web tier's gunicorn workers calling cache.set via its own
    RPC path), the existing inline behaviour applies.
    """
    # Route through the WriterThread when an active writer is registered
    # in the calling context. In production this is set by the broker's
    # serve() lifecycle (warm handlers use this path). Outside the broker
    # (web tier, tests that don't register a writer) get_active_writer()
    # raises RuntimeError and the code falls through to the direct path.
    try:
        from mimir.broker import _context

        _writer = _context.get_active_writer()
    except RuntimeError:
        _writer = None

    if _writer is not None:
        # Fire-and-forget; matches cache.set's best-effort posture.
        # Local import avoids a module-level circular dependency
        # between cache.py and broker.writes (which imports cache).
        from mimir.cache import set_via_writer  # noqa: PLC0415

        set_via_writer(_writer, key, value, ttl)
        return

    nskey = _ns(key)
    payload = json.dumps(_encode(value), separators=(",", ":"))
    if len(payload) > MAX_CACHE_VALUE_BYTES:
        # Refuse the write rather than letting it land. A multi-MB
        # row would amplify into both the broker wire path (16 MiB
        # cap) and every cache.get reader's memory; a clean log
        # surface is the right signal for the caller to investigate.
        logger.warning(
            "cache write for %s exceeds %d bytes (%d); dropped",
            nskey,
            MAX_CACHE_VALUE_BYTES,
            len(payload),
        )
        return
    if _should_dispatch_to_broker():
        # Import locally so the cache module doesn't depend on the
        # broker package at import time (avoids a cycle through
        # `mimir.broker.client` → `mimir.cache` for tests that stub
        # the client out, and lets unit tests for the cache module
        # run without spinning up a broker).
        from mimir.broker.client import BrokerUnavailable, get_broker_client

        try:
            get_broker_client().cache_set(nskey, payload, ttl)
        except BrokerUnavailable as exc:
            logger.warning("broker write failed for %s: %s", nskey, exc)
        return
    try:
        _direct_set(nskey, payload, ttl)
    except OperationalError as exc:
        logger.warning("cache write failed for %s: %s", nskey, exc)


def set_via_writer(writer: Any, key: str, value: Any, ttl: int) -> Any:
    """Phase 2 of the two-pool restructure
    (`_claude/specs/2026-05-29-broker-two-pool-design.md`).

    Compose a WriteOp wrapping the same UPSERT that `_direct_set()` runs
    today, submit it to the given WriterThread, and return the WriteFuture.
    Fire-and-forget callers ignore the future; the rare caller that wants a
    post-commit guarantee (e.g. an admin op) blocks on `.result()`.

    Serialization (key namespace prefix, value encoder) MUST match
    `set()` exactly so rows written here are readable via `get()`
    and `get_or_compute()`.

    Used by warm handlers in Phase 2. Other callers (web-tier RPC
    handler, long-ops, admin) still use `set()`; their migration
    is Phase 4 / Phase 5.
    """
    from sqlalchemy import text

    from mimir.broker.writes import WriteOp

    nskey = _ns(key)
    payload = json.dumps(_encode(value), separators=(",", ":"))
    expires_at = _now() + ttl

    def _fn(conn: Any) -> None:
        conn.execute(
            text(
                "INSERT INTO cache (key, value, expires_at) "
                "VALUES (:k, :v, :e) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, expires_at = excluded.expires_at"
            ),
            {"k": nskey, "v": payload, "e": expires_at},
        )

    return writer.submit(WriteOp(label=f"cache.set:{key}", fn=_fn))


def get_or_compute(
    session: Session,
    key: str,
    ttl: int,
    fn: Callable[[], Any],
    *,
    force: bool = False,
) -> Any:
    """Cache-aside fetch riding on the caller's session for the read.

    On hit (and not `force`), returns the decoded value without
    opening a fresh connection. On miss, calls `fn()` and persists
    the result via `set()` (which owns its own session, so the
    caller's transaction stays untouched). Saves one connection per
    hit and one per miss vs separate `get()` + `set()` calls.
    """
    nskey = _ns(key)
    if not force:
        row = session.execute(
            select(CacheEntry.value, CacheEntry.expires_at).where(
                CacheEntry.key == nskey
            )
        ).one_or_none()
        if row is not None and row.expires_at >= _now():
            window = _refresh_within.get()
            if window is None or (row.expires_at - _now()) >= window:
                return _decode(json.loads(row.value))
            # Inside an active refresh_window and the row is about to
            # expire: fall through to recompute instead of serving a
            # near-stale value that the next user request would miss.
    value = fn()
    set(key, value, ttl)
    return value


def keys() -> list[str]:
    """Non-expired keys, with the namespace prefix stripped so callers
    see the original key they passed in."""
    now = _now()
    prefix = f"v{NAMESPACE_VERSION}:"
    with SessionLocal() as session:
        return [
            (k[len(prefix) :] if k.startswith(prefix) else k)
            for (k,) in session.execute(
                select(CacheEntry.key).where(CacheEntry.expires_at >= now)
            )
        ]


def _direct_purge_expired() -> int:
    """Direct-SQLite delete of every expired row. Used by
    `purge_expired()` when broker mode is off, and by
    `mimir.broker.handlers` (the broker daemon's internal periodic
    purge timer runs this against its own writer connection)."""
    now = _now()
    with SessionLocal() as session:
        result = session.execute(
            delete_stmt(CacheEntry).where(CacheEntry.expires_at < now)
        )
        session.commit()
        return result.rowcount or 0


def purge_expired() -> int:
    """Drop every expired row. Returns rows deleted. Cheap thanks to
    `ix_cache_expires_at`.

    Forwards to the broker daemon, which owns the cache table's data
    lifecycle end-to-end (writes + purge). On `BrokerUnavailable`,
    log + return 0; the broker's own internal periodic purge thread
    will catch up.
    """
    if _should_dispatch_to_broker():
        from mimir.broker.client import BrokerUnavailable, get_broker_client

        try:
            return get_broker_client().cache_purge_expired()
        except BrokerUnavailable as exc:
            logger.warning("broker purge_expired failed: %s", exc)
            return 0
    return _direct_purge_expired()


def _direct_delete(nskey: str) -> int:
    """Direct-SQLite delete of one cache row. Used by `delete()`
    inside the broker process, and by `mimir.broker.handlers`."""
    with SessionLocal() as session:
        result = session.execute(delete_stmt(CacheEntry).where(CacheEntry.key == nskey))
        session.commit()
        return result.rowcount or 0


def delete(key: str) -> int:
    """Drop the cache row for one key, if present. Returns rows
    deleted (0 or 1, or 0 on transient-lock fallback).

    Best-effort just like `set()`: SQLite write contention during
    a long-running VACUUM raises `OperationalError("database is
    locked")` once the connection's `busy_timeout` elapses. Admin
    CRUD callers (`mimir.inboxes`) would otherwise 500 on a
    successfully-completed DB change just because the cache
    invalidation lost the lock race. Log + return 0; the cached
    value still ages out via TTL and a successful subsequent write
    overwrites whatever stale row survived.

    Forwards to the broker daemon via RPC.
    `BrokerUnavailable` is logged + returns 0 (cached value still
    ages out via TTL).
    """
    nskey = _ns(key)
    if _should_dispatch_to_broker():
        from mimir.broker.client import BrokerUnavailable, get_broker_client

        try:
            return get_broker_client().cache_delete(nskey)
        except BrokerUnavailable as exc:
            logger.warning("broker delete failed for %s: %s", nskey, exc)
            return 0
    try:
        return _direct_delete(nskey)
    except OperationalError as exc:
        logger.warning("cache delete failed for %s: %s", nskey, exc)
        return 0


def _direct_delete_for_inbox(inbox_name: str) -> int:
    """Direct-SQLite delete of every cache entry whose key references
    `inbox_name`. Used by `delete_for_inbox()` inside the broker
    process, and by `mimir.broker.handlers`."""
    suffix_pat = f"%:{inbox_name}"
    middle_pat = f"%:{inbox_name}:%"
    with SessionLocal() as session:
        result = session.execute(
            delete_stmt(CacheEntry).where(
                or_(
                    CacheEntry.key.like(suffix_pat),
                    CacheEntry.key.like(middle_pat),
                )
            )
        )
        session.commit()
        return result.rowcount or 0


def delete_for_inbox(inbox_name: str) -> int:
    """Drop every cache entry whose key references `inbox_name`.

    Cache keys follow the convention `<helper>:<inbox_name>[:<rest>]`,
    so an entry references an inbox if its key ends with `:{name}` or
    contains `:{name}:`. Called by the admin CRUD layer after a
    rename or delete so reads don't return rows pointing at a now-
    stale (or vanished) name. Returns rows deleted (0 on transient-
    lock fallback; same best-effort posture as `set()` / `delete()`).

    Inbox names are slug-validated (alphanumeric + hyphen) so they
    contain no LIKE-pattern metacharacters; the literal `%` and `_`
    cases that would need escaping can't occur.

    Forwards to the broker daemon via RPC.
    `BrokerUnavailable` is logged + returns 0.
    """
    if _should_dispatch_to_broker():
        from mimir.broker.client import BrokerUnavailable, get_broker_client

        try:
            return get_broker_client().cache_delete_for_inbox(inbox_name)
        except BrokerUnavailable as exc:
            logger.warning(
                "broker delete_for_inbox failed for %s: %s",
                inbox_name,
                exc,
            )
            return 0
    try:
        return _direct_delete_for_inbox(inbox_name)
    except OperationalError as exc:
        logger.warning(
            "cache delete_for_inbox failed for %s: %s",
            inbox_name,
            exc,
        )
        return 0
