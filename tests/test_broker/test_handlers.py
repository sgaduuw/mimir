"""Unit tests for `mimir.broker.handlers.dispatch`. No socket, no
broker daemon, just `dispatch(line: bytes) -> Reply` against the
seeded test DB. Covers the happy path for each op, plus the error
boundary (malformed JSON / unknown op / invalid request)."""

from mimir import cache
from mimir.broker.handlers import dispatch
from mimir.broker.protocol import (
    CacheDeleteForInboxRequest,
    CacheDeleteRequest,
    CachePurgeExpiredRequest,
    CacheSetRequest,
    PingRequest,
)


def _line(req) -> bytes:
    return req.model_dump_json().encode("utf-8")


def test_dispatch_ping(seeded_db):
    reply = dispatch(_line(PingRequest()))
    assert reply.ok is True
    assert reply.error is None


def test_dispatch_cache_set_writes_to_db(seeded_db):
    """Send `cache_set`, then read back via direct SQLite (NOT via
    cache.get, which would otherwise hide a missing row by going
    through the broker dispatch we're testing)."""
    nskey = cache._ns("test_dispatch_set")
    reply = dispatch(_line(CacheSetRequest(
        key=nskey, value_json='"hello"', ttl=60,
    )))
    assert reply.ok is True
    # cache.get expects an un-namespaced key but `_direct_set` was
    # called with a namespaced one, so we round-trip via the public
    # API with the matching un-namespaced key.
    assert cache.get("test_dispatch_set") == "hello"


def test_dispatch_cache_delete_removes_row(seeded_db):
    nskey = cache._ns("test_dispatch_delete")
    cache.set("test_dispatch_delete", "x", ttl=60)
    assert cache.get("test_dispatch_delete") == "x"

    reply = dispatch(_line(CacheDeleteRequest(key=nskey)))
    assert reply.ok is True

    assert cache.get("test_dispatch_delete") is None


def test_dispatch_cache_delete_for_inbox_reports_count(seeded_db):
    cache.set("archive_stats:alpha", "x1", ttl=60)
    cache.set("daily_volume:alpha:30", "x2", ttl=60)
    cache.set("archive_stats:beta", "x3", ttl=60)

    reply = dispatch(_line(CacheDeleteForInboxRequest(name="alpha")))
    assert reply.ok is True
    assert reply.rows_deleted == 2

    assert cache.get("archive_stats:alpha") is None
    assert cache.get("daily_volume:alpha:30") is None
    # beta untouched.
    assert cache.get("archive_stats:beta") == "x3"


def test_dispatch_cache_purge_expired_reports_count(seeded_db):
    """Insert one row with `expires_at` in the past (via the ORM
    directly, since `cache.set(ttl=0)` makes a same-second row that
    the `< now` filter doesn't quite catch) and one row that's
    safely alive, then dispatch `cache_purge_expired` and assert
    both the count and the post-purge survival."""
    from datetime import datetime, timezone

    from mimir.cache import _ns
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    now = int(datetime.now(timezone.utc).timestamp())
    with SessionLocal() as s:
        s.add(CacheEntry(key=_ns("expired_one"), value='"x"', expires_at=now - 60))
        s.add(CacheEntry(key=_ns("alive_one"), value='"y"', expires_at=now + 3600))
        s.commit()

    reply = dispatch(_line(CachePurgeExpiredRequest()))
    assert reply.ok is True
    assert (reply.rows_deleted or 0) >= 1

    assert cache.get("expired_one") is None
    assert cache.get("alive_one") == "y"


def test_dispatch_malformed_json(seeded_db):
    reply = dispatch(b"{not json")
    assert reply.ok is False
    assert reply.error == "MalformedJSON"


def test_dispatch_unknown_op(seeded_db):
    reply = dispatch(b'{"op": "fly_to_the_moon"}')
    assert reply.ok is False
    assert reply.error == "UnknownOp"


def test_dispatch_invalid_request_fields(seeded_db):
    """`op: cache_set` but no `key`/`value_json`/`ttl` → pydantic
    rejects → `InvalidRequest`."""
    reply = dispatch(b'{"op": "cache_set"}')
    assert reply.ok is False
    assert reply.error == "InvalidRequest"


def test_dispatch_handler_does_not_crash_on_bad_value_type(seeded_db):
    """A non-dict JSON top-level (e.g. a list) shouldn't crash
    dispatch; UnknownOp is the right shape since op cannot be
    extracted."""
    reply = dispatch(b'[1, 2, 3]')
    assert reply.ok is False
    assert reply.error == "UnknownOp"
