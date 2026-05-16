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
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterator

from pydantic import BaseModel
from sqlalchemy import delete as delete_stmt, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from mimir.extensions import SessionLocal
from mimir.models import CacheEntry

logger = logging.getLogger(__name__)

# Bump when cached value shapes change (query rewrites, dataclass
# field renames, encoder changes). Old rows fall through to a miss
# and get cleaned up by `purge_expired`.
NAMESPACE_VERSION = 2

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
                f.name: _encode(getattr(obj, f.name))
                for f in dataclasses.fields(obj)
            }
        elif isinstance(obj, BaseModel):
            # Iterate via `model_fields` + getattr so nested BaseModel /
            # dataclass values recurse through `_encode` (which knows
            # the type tags), instead of through `model_dump()` which
            # would flatten them to plain dicts and lose the tags.
            fields = {
                name: _encode(getattr(obj, name))
                for name in type(obj).model_fields
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


def get(key: str) -> Any:
    nskey = _ns(key)
    now = _now()
    with SessionLocal() as session:
        row = session.execute(
            select(CacheEntry.value, CacheEntry.expires_at).where(CacheEntry.key == nskey)
        ).one_or_none()
    if row is None or row.expires_at < now:
        return None
    return _decode(json.loads(row.value))


def set(key: str, value: Any, ttl: int) -> None:
    """Best-effort cache write.

    SQLite write contention (scheduler ingest / vacuum overlapping a
    request) raises `OperationalError("database is locked")` once the
    `busy_timeout` window elapses. The page already rendered before
    we got here, so a failed cache write must not propagate, it'd
    500 a successful response. Log and move on; the next request
    recomputes.
    """
    nskey = _ns(key)
    payload = json.dumps(_encode(value), separators=(",", ":"))
    expires_at = _now() + ttl
    stmt = (
        sqlite_insert(CacheEntry)
        .values(key=nskey, value=payload, expires_at=expires_at)
        .on_conflict_do_update(
            index_elements=["key"],
            set_={"value": payload, "expires_at": expires_at},
        )
    )
    try:
        with SessionLocal() as session:
            session.execute(stmt)
            session.commit()
    except OperationalError as exc:
        logger.warning("cache write failed for %s: %s", nskey, exc)


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
            select(CacheEntry.value, CacheEntry.expires_at).where(CacheEntry.key == nskey)
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
            (k[len(prefix):] if k.startswith(prefix) else k)
            for k, in session.execute(
                select(CacheEntry.key).where(CacheEntry.expires_at >= now)
            )
        ]


def purge_expired() -> int:
    """Drop every expired row. Returns rows deleted. Cheap thanks to
    `ix_cache_expires_at`."""
    now = _now()
    with SessionLocal() as session:
        result = session.execute(delete_stmt(CacheEntry).where(CacheEntry.expires_at < now))
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
    """
    nskey = _ns(key)
    try:
        with SessionLocal() as session:
            result = session.execute(
                delete_stmt(CacheEntry).where(CacheEntry.key == nskey)
            )
            session.commit()
            return result.rowcount or 0
    except OperationalError as exc:
        logger.warning("cache delete failed for %s: %s", nskey, exc)
        return 0


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
    """
    suffix_pat = f"%:{inbox_name}"
    middle_pat = f"%:{inbox_name}:%"
    try:
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
    except OperationalError as exc:
        logger.warning(
            "cache delete_for_inbox failed for %s: %s", inbox_name, exc,
        )
        return 0
