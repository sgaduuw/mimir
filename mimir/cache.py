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
and age out via the broker's periodic purge. Callers don't see the prefix.
"""

import dataclasses
import json
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import delete as delete_stmt
from sqlalchemy import or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from mimir.config import settings
from mimir.extensions import SessionLocal
from mimir.models import CacheEntry

logger = logging.getLogger(__name__)

# Bump when cached value shapes change (query rewrites, dataclass
# field renames, encoder changes). Old rows fall through to a miss
# and get cleaned up by the broker's periodic purge. A change to a registered
# type's fields fails `test_a_cached_field_change_bumps_the_namespace`
# until this is bumped: an older release reads the cache on rollback.
# Plain container values and encoder changes still need a bump by hand.
NAMESPACE_VERSION = 6

# Warm handlers use one window for proactive refresh and stored-TTL insurance.
# None keeps ordinary request reads and writes at their nominal lifetime.
_warm_window: ContextVar[int | None] = ContextVar("_warm_window", default=None)


@contextmanager
def warm_window(seconds: int | None) -> Iterator[None]:
    """Couple warm refresh decisions and TTL extension within this scope.

    Each broker warm worker enters its own context. Write paths snapshot
    the extended TTL before an RPC or writer submission, since those
    boundaries do not transport the caller's context.
    """
    token = _warm_window.set(seconds)
    try:
        yield
    finally:
        _warm_window.reset(token)


def _apply_ttl_extension(ttl: int) -> int:
    """Snapshot the warm window before crossing a write boundary."""
    return ttl + (_warm_window.get() or 0)


def _ns(key: str) -> str:
    """Apply the cache namespace prefix to a caller-supplied key."""
    return f"v{NAMESPACE_VERSION}:{key}"


# Tag picked explicitly (not `cls.__name__`) so a rename in the source
# can't silently invalidate previously-cached rows or, worse, decode
# them into the wrong type.
#
# Thread-safety invariant: `_TYPES` and `_TAGS` are populated only at
# module import time by top-level `cache.register("Tag", Cls)` calls
# in each owning module (`mimir/dashboard.py`, `mimir/threading.py`,
# `mimir/subsystems.py`, etc.). Python's import lock serialises module
# initialisation, so these writes never race. Reads from `_encode` and
# `_decode` happen on broker worker threads at runtime, after every
# module has finished importing — so reads only ever see a frozen
# dict.
#
# **DO NOT add a `cache.register(...)` call inside a function body or
# any code path that runs after import.** Under free-threaded Python
# (`PYTHON_GIL=0` in production) a late `register()` call would race
# concurrent `_encode` / `_decode` reads on a worker thread; a
# dict-resize race can corrupt the hash table or raise
# `RuntimeError: dictionary changed size during iteration`. The audit
# at 2026-06-12 (issue #471) confirmed all 17 callsites are at
# module top level; any new caller should land in that pattern too.
_TYPES: dict[str, type] = {}
_TAGS: dict[type, str] = {}


def register(tag: str, cls: type) -> None:
    """Make `cls` round-trip through cache encode/decode under `tag`.

    Call only at module import time. See the comment above `_TYPES` /
    `_TAGS` for the thread-safety invariant.
    """
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
    if dataclasses.is_dataclass(cls):
        # A row another release wrote can carry a field this one lacks
        # (#844 dropped `PatchState.mainline_landings`) and is read within
        # its TTL. Dropped before decoding, since the value's own tag may
        # be gone too. This does not make field changes safe: a renamed
        # field decodes as its default, or raises without one. Pydantic
        # models ignore an extra scalar, but each value is decoded first,
        # so a removed field holding a nested tag still raises. Hence a
        # NAMESPACE_VERSION bump for every field change.
        names = {f.name for f in dataclasses.fields(cls)}
        v = {k: val for k, val in v.items() if k in names}
    return cls(**{k: _decode(val) for k, val in v.items()})


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _should_dispatch_to_broker() -> bool:
    """True iff this process should send cache writes to the broker.
    False inside the broker itself, either by the process-level
    `MIMIR_IS_BROKER` marker or the per-thread "inside a broker
    handler" flag (the in-process test shape, where the env marker is
    shared): RPC'ing to ourselves would be a pointless round-trip.
    Otherwise True, unconditionally: the broker is the sole writer
    (2.0.0), so every other process routes its writes through it.
    """
    return not (settings.mimir_is_broker or _broker_handler_active())


# Thread-local flag set by `mimir.broker.server`'s worker loops
# while they're dispatching a request. `cache._should_dispatch_to_broker`
# checks it (in addition to `settings.mimir_is_broker`) to avoid a
# self-RPC when broker + caller share a process (the in-process
# test shape).
#
# Landmine to watch for (audit #486): thread-locals are scoped to the
# thread that sets them. A handler that spawns helper threads (e.g. a
# `ThreadPoolExecutor` inside a warm target) will see those threads
# inherit no `_HANDLER_TLS.active = True` flag, so any `cache.set` from
# inside the helper falls through to `_should_dispatch_to_broker() ->
# True` and attempts a self-RPC back through the broker socket. With
# the cache worker already busy dispatching the parent RPC, that
# self-RPC sits on the queue forever -> deadlock.
#
# Today no handler spawns helper threads; this is a design landmine,
# not a current bug. If that ever changes, either bracket the helper's
# entry/exit with `_set_broker_handler_active(True)` / `(False)`, or
# (preferred) factor the cache writes back into the dispatching
# thread.
_HANDLER_TLS = threading.local()


def _broker_handler_active() -> bool:
    return getattr(_HANDLER_TLS, "active", False)


def _set_broker_handler_active(active: bool) -> None:
    _HANDLER_TLS.active = active


def alive(session: Session, key: str) -> bool:
    """Whether `key` holds an unexpired entry, without reading or
    decoding the value. For process-local derivations of a cached value
    that must die with it (`subsystems._rules_index`)."""
    expires_at = session.scalar(
        select(CacheEntry.expires_at).where(CacheEntry.key == _ns(key))
    )
    return expires_at is not None and expires_at >= _now()


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


def set(key: str, value: Any, ttl: int) -> None:
    """Best-effort cache write.

    Outside the broker, sends the write one-way (`CacheSetRequest`) and
    returns as soon as it is written to the socket: nothing waits on
    the broker's queue or commit. A `BrokerUnavailable` on send is
    logged and swallowed: the page already rendered before we got here,
    so a failed cache write must not propagate, it'd 500 a successful
    response. Next request recomputes.

    Inside the broker the write goes through the active WriterThread,
    the sole writer, so there is no inline or shared-engine path. A
    broker handler thread fires and forgets, matching the best-effort
    posture. Any other broker-side caller (in practice only tests,
    which run with MIMIR_IS_BROKER set) blocks on the result so a read
    right after the write sees it.

    Every path enforces `MAX_CACHE_VALUE_BYTES` first: the check sits
    before the branching so no caller can route around it.
    """
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
        from mimir.broker.protocol import CacheSetRequest

        # Apply the extension here, before crossing the wire: the
        # broker's _warm_window ContextVar is unset in the cache-
        # worker thread (ContextVar is per-thread; the warm worker's
        # scope doesn't leak into other broker threads), so the
        # broker-side _apply_ttl_extension is a no-op for RPCs from
        # web tier. The web tier's extension must be baked in here or
        # it's silently dropped at the wire.
        wire_ttl = _apply_ttl_extension(ttl)
        try:
            get_broker_client().send(
                CacheSetRequest(key=nskey, value_json=payload, ttl=wire_ttl)
            )
        except BrokerUnavailable as exc:
            logger.warning("broker write failed for %s: %s", nskey, exc)
        return
    from mimir.broker import _context

    # `_set_via_writer_for_nskey` applies the TTL extension itself.
    # The thread-local flag, not mere writer registration, picks the
    # fire-and-forget branch: test threads share the process with a
    # registered writer but must be able to read back what they wrote.
    future = _set_via_writer_for_nskey(
        _context.get_active_writer(), nskey, payload, ttl
    )
    if not _broker_handler_active():
        future.result()


def _set_via_writer_for_nskey(
    writer: Any, nskey: str, value_json: str, ttl: int, *, block: bool = True
) -> Any:
    """Submit the cache UPSERT for a pre-namespaced key and a
    pre-JSON-encoded payload to the writer and return the WriteFuture.

    Callers: `set` (inside the broker) and the broker's
    `handle_cache_set`, which receives both on the wire from a web
    tier that already namespaced + encoded. Performs no size check;
    `set` owns that. Closure returns None. `block=False` raises
    `queue.Full` rather than wait on a full writer queue.
    """
    from mimir.broker.writes import WriteOp

    # Apply any active warm-cache TTL extension at submit time so the
    # value snapshot the writer-thread closure captures is the bumped
    # one; the WriterThread may run the closure on a different thread,
    # where the caller's ContextVar isn't visible.
    ttl = _apply_ttl_extension(ttl)
    expires_at = _now() + ttl

    def _fn(conn: Any) -> None:
        conn.execute(
            sqlite_insert(CacheEntry)
            .values(key=nskey, value=value_json, expires_at=expires_at)
            .on_conflict_do_update(
                index_elements=["key"],
                set_={"value": value_json, "expires_at": expires_at},
            )
        )

    return writer.submit(WriteOp(label=f"cache.set:{nskey}", fn=_fn), block=block)


def _submit_stmt(writer: Any, label: str, stmt: Any, *, block: bool = True) -> Any:
    """Submit one DELETE statement as a WriteOp. The future's result is
    the rowcount."""
    from mimir.broker.writes import WriteOp

    def _fn(conn: Any) -> int:
        return conn.execute(stmt).rowcount or 0

    return writer.submit(WriteOp(label=label, fn=_fn), block=block)


def delete_via_writer(writer: Any, nskey: str, *, block: bool = True) -> Any:
    """Compose a WriteOp wrapping the single-key cache DELETE. The
    future's result is the rowcount.

    `nskey` is the already-namespaced key; callers with a raw key
    should call `_ns(key)` first.
    """
    return _submit_stmt(
        writer,
        f"cache.delete:{nskey}",
        delete_stmt(CacheEntry).where(CacheEntry.key == nskey),
        block=block,
    )


def delete_for_inbox_via_writer(
    writer: Any, inbox_name: str, *, block: bool = True
) -> Any:
    """Compose a WriteOp wrapping the per-inbox cache DELETE. The
    future's result is the rowcount.

    Keys following the convention `<helper>:<inbox_name>[:<rest>]`
    match if they end with `:{name}` or contain `:{name}:`.
    """
    return _submit_stmt(
        writer,
        f"cache.delete_for_inbox:{inbox_name}",
        delete_stmt(CacheEntry).where(
            or_(
                CacheEntry.key.like(f"%:{inbox_name}"),
                CacheEntry.key.like(f"%:{inbox_name}:%"),
            )
        ),
        block=block,
    )


def purge_expired_via_writer(writer: Any, *, block: bool = True) -> Any:
    """Compose a WriteOp wrapping the expired-row DELETE. The future's
    result is the rowcount.

    `_now()` is read here, at submit time, so the cutoff is fixed; rows
    that expire in the interim land on the next purge cycle.
    """
    return _submit_stmt(
        writer,
        "cache.purge_expired",
        delete_stmt(CacheEntry).where(CacheEntry.expires_at < _now()),
        block=block,
    )


def get_or_compute(
    session: Session,
    key: str,
    ttl: int,
    fn: Callable[[], Any],
    *,
    force: bool = False,
    retain_for: int = 0,
) -> Any:
    """Cache-aside fetch riding on the caller's session for the read.

    On hit (and not `force`), returns the decoded value without
    opening a fresh connection. On miss, calls `fn()` and persists
    the result via `set()` (which owns its own session, so the
    caller's transaction stays untouched). `retain_for` keeps the last
    value available to read-only callers after its refresh deadline.
    """
    nskey = _ns(key)
    if not force:
        row = session.execute(
            select(CacheEntry.value, CacheEntry.expires_at).where(
                CacheEntry.key == nskey
            )
        ).one_or_none()
        remaining = row.expires_at - _now() - retain_for if row else -1
        if remaining >= 0:
            window = _warm_window.get()
            if window is None:
                return _decode(json.loads(row.value))
            # Probabilistic refresh window, only reached inside a
            # warm-cache cycle (window is None by default). The
            # bands are measured from the row's own clock, so a
            # stored extension does not alter the nominal TTL passed below.
            from mimir.cache_warm import should_refresh

            if not should_refresh(
                nominal_ttl=ttl,
                window_sec=int(window),
                remaining=int(remaining),
            ):
                return _decode(json.loads(row.value))
            # Probabilistic / deterministic check said yes: fall
            # through to recompute.
    value = fn()
    set(key, value, ttl + retain_for)
    return value


def keys() -> list[str]:
    """Non-expired keys, with the namespace prefix stripped so callers
    see the original key they passed in."""
    now = _now()
    prefix = f"v{NAMESPACE_VERSION}:"
    with SessionLocal() as session:
        return [
            (k.removeprefix(prefix))
            for (k,) in session.execute(
                select(CacheEntry.key).where(CacheEntry.expires_at >= now)
            )
        ]


def delete(key: str) -> int:
    """Drop the cache row for one key, if present, and return rows
    deleted (0 or 1) once the delete has committed.

    Broker-only: every caller (the update-mainline reload) runs inside
    the broker, so the delete goes straight to the active WriterThread.
    Elsewhere `get_active_writer()` raises, which is the single-writer
    invariant doing its job, not a path to route around."""
    from mimir.broker import _context

    return delete_via_writer(_context.get_active_writer(), _ns(key)).result()


def delete_for_inbox(inbox_name: str) -> int:
    """Drop every cache entry whose key references `inbox_name`, and
    return rows deleted once the delete has committed.

    Cache keys follow the convention `<helper>:<inbox_name>[:<rest>]`,
    so an entry references an inbox if its key ends with `:{name}` or
    contains `:{name}:`. Called by the admin CRUD layer after a
    rename or delete, and by ingest on an inbox's first rows, so reads
    don't return rows pointing at a stale (or vanished) name.

    Inbox names are slug-validated (alphanumeric + hyphen) so they
    contain no LIKE-pattern metacharacters; the literal `%` and `_`
    cases that would need escaping can't occur.

    Broker-only, like `delete`."""
    from mimir.broker import _context

    return delete_for_inbox_via_writer(
        _context.get_active_writer(), inbox_name
    ).result()
