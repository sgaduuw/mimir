"""Unit tests for `mimir.broker.handlers.dispatch`. No socket, no
broker daemon, just `dispatch(line: bytes) -> Reply` against the
seeded test DB. Covers the happy path for each op, plus the error
boundary (malformed JSON / unknown op / invalid request)."""

import time

from mimir import cache
from mimir.broker.handlers import dispatch
from mimir.broker.protocol import (
    CacheSetRequest,
    PingRequest,
)


def _line(req) -> bytes:
    return req.model_dump_json().encode("utf-8")


def test_dispatch_ping(seeded_db):
    reply = dispatch(_line(PingRequest(rpc_id=1)))
    assert reply.ok is True
    assert reply.error is None


def test_dispatch_cache_set_writes_to_db(seeded_db):
    """Send `cache_set`, then read back via direct SQLite (NOT via
    cache.get, which would otherwise hide a missing row by going
    through the broker dispatch we're testing)."""
    nskey = cache._ns("test_dispatch_set")
    reply = dispatch(
        _line(
            CacheSetRequest(
                rpc_id=1,
                key=nskey,
                value_json='"hello"',
                ttl=60,
            )
        )
    )
    assert reply.ok is True
    # The reply goes out once the write is queued (#1157), so wait for
    # the commit. The request carried a namespaced key, so round-trip
    # via the public API with the matching un-namespaced key.
    deadline = time.monotonic() + 5
    while cache.get("test_dispatch_set") is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cache.get("test_dispatch_set") == "hello"


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
    reply = dispatch(b"[1, 2, 3]")
    assert reply.ok is False
    assert reply.error == "UnknownOp"


def test_dispatch_echoes_rpc_id_on_success():
    """dispatch() attaches the request's rpc_id to the Reply so
    the client can demux multiple in-flight RPCs."""
    line = b'{"op":"ping","rpc_id":12345}'
    reply = dispatch(line)
    assert reply.ok is True
    assert reply.rpc_id == 12345


def test_dispatch_echoes_rpc_id_on_handler_error():
    """rpc_id is echoed even when the handler returns ok=False, so
    the client can resolve the future cleanly."""
    # Unknown op produces Reply(ok=False, error="UnknownOp").
    line = b'{"op":"no_such_op","rpc_id":99}'
    reply = dispatch(line)
    assert reply.ok is False
    assert reply.rpc_id == 99


def test_dispatch_malformed_json_returns_rpc_id_zero():
    """Malformed JSON: no rpc_id is parseable, so the Reply uses 0.
    The client allocates from 1, so 0 lookups miss and the reply is
    silently dropped."""
    reply = dispatch(b"this is not json")
    assert reply.ok is False
    assert reply.error == "MalformedJSON"
    assert reply.rpc_id == 0


def test_dispatch_missing_rpc_id_returns_rpc_id_zero():
    """Request JSON without the required rpc_id field fails pydantic
    validation; the Reply uses 0 as the rpc_id sentinel."""
    reply = dispatch(b'{"op":"ping"}')  # no rpc_id
    assert reply.ok is False
    assert reply.error == "InvalidRequest"
    assert reply.rpc_id == 0


def test_dispatch_negative_rpc_id_returns_rpc_id_zero():
    """Negative rpc_id in malformed request: dispatch coerces to 0
    so the Reply does not escape pydantic's Field(ge=0) constraint.
    Pins the guard added to keep dispatch() within its 'never raises'
    contract."""
    reply = dispatch(b'{"op":"no_such_op","rpc_id":-5}')
    assert reply.ok is False
    assert reply.rpc_id == 0
