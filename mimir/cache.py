"""DB-backed cache for slow dashboard queries.

Cached values are JSON-encoded and stored in the `cache` table, keyed
by string with a unix-second expiry. SQLite's WAL lock makes the
multi-process case (Flask server + warm-cache cron) atomic without
any extra coordination.

Only types registered via `register(tag, cls)` round-trip cleanly.
Each module owning a cached dataclass calls `register` at import time;
the dependency stays one-way (cache knows nothing about its callers).
"""
import dataclasses
import json
from datetime import date, datetime, timezone
from typing import Any, Callable

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from mimir.extensions import SessionLocal
from mimir.models import CacheEntry

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
        return {
            "__t": tag,
            "v": {f.name: _encode(getattr(obj, f.name)) for f in dataclasses.fields(obj)},
        }
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
    now = _now()
    with SessionLocal() as session:
        row = session.execute(
            select(CacheEntry.value, CacheEntry.expires_at).where(CacheEntry.key == key)
        ).one_or_none()
    if row is None or row.expires_at < now:
        return None
    return _decode(json.loads(row.value))


def set(key: str, value: Any, ttl: int) -> None:
    payload = json.dumps(_encode(value), separators=(",", ":"))
    expires_at = _now() + ttl
    stmt = (
        sqlite_insert(CacheEntry)
        .values(key=key, value=payload, expires_at=expires_at)
        .on_conflict_do_update(
            index_elements=["key"],
            set_={"value": payload, "expires_at": expires_at},
        )
    )
    with SessionLocal() as session:
        session.execute(stmt)
        session.commit()


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
    if not force:
        row = session.execute(
            select(CacheEntry.value, CacheEntry.expires_at).where(CacheEntry.key == key)
        ).one_or_none()
        if row is not None and row.expires_at >= _now():
            return _decode(json.loads(row.value))
    value = fn()
    set(key, value, ttl)
    return value


def keys() -> list[str]:
    """Non-expired keys. Useful for `warm-cache` reporting."""
    now = _now()
    with SessionLocal() as session:
        return [
            k for k, in session.execute(
                select(CacheEntry.key).where(CacheEntry.expires_at >= now)
            )
        ]


def purge_expired() -> int:
    """Drop every expired row. Returns rows deleted. Cheap thanks to
    `ix_cache_expires_at`."""
    now = _now()
    with SessionLocal() as session:
        result = session.execute(delete(CacheEntry).where(CacheEntry.expires_at < now))
        session.commit()
        return result.rowcount or 0


def delete_for_inbox(inbox_name: str) -> int:
    """Drop every cache entry whose key references `inbox_name`.

    Cache keys follow the convention `<helper>:<inbox_name>[:<rest>]`,
    so an entry references an inbox if its key ends with `:{name}` or
    contains `:{name}:`. Called by the admin CRUD layer after a
    rename or delete so reads don't return rows pointing at a now-
    stale (or vanished) name. Returns rows deleted.

    Inbox names are slug-validated (alphanumeric + hyphen) so they
    contain no LIKE-pattern metacharacters; the literal `%` and `_`
    cases that would need escaping can't occur.
    """
    suffix_pat = f"%:{inbox_name}"
    middle_pat = f"%:{inbox_name}:%"
    with SessionLocal() as session:
        result = session.execute(
            delete(CacheEntry).where(
                or_(
                    CacheEntry.key.like(suffix_pat),
                    CacheEntry.key.like(middle_pat),
                )
            )
        )
        session.commit()
        return result.rowcount or 0
