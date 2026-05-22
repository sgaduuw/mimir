"""Wire-message round-trips for `mimir.broker.protocol`. The
broker and client both depend on these models parsing the bytes
the OTHER side produced; a mismatch (forgot a field, renamed an
op tag, changed a discriminator) is the kind of bug that doesn't
surface in unit tests of either side alone."""

import json

import pytest
from pydantic import ValidationError

from mimir.broker.protocol import (
    BootstrapInboxesRequest,
    CacheDeleteForInboxRequest,
    CacheDeleteRequest,
    CachePurgeExpiredRequest,
    CacheSetRequest,
    PingRequest,
    Reply,
)


def test_cache_set_round_trip():
    req = CacheSetRequest(key="v2:archive_stats:lkml", value_json='{"x":1}', ttl=86400)
    encoded = req.model_dump_json()
    parsed = CacheSetRequest.model_validate_json(encoded)
    assert parsed.key == req.key
    assert parsed.value_json == req.value_json
    assert parsed.ttl == req.ttl
    assert parsed.op == "cache_set"


def test_cache_set_rejects_empty_value_json():
    """Empty `value_json` would JSON-decode to `null`, which makes
    cache.get on hit return `None` and look like a miss. Boundary
    rejection keeps useless rows out of the cache."""
    with pytest.raises(ValidationError):
        CacheSetRequest(key="x", value_json="", ttl=60)


def test_cache_set_rejects_negative_ttl():
    with pytest.raises(ValidationError):
        CacheSetRequest(key="x", value_json='"v"', ttl=-1)


def test_cache_delete_round_trip():
    req = CacheDeleteRequest(key="v2:thing:y")
    parsed = CacheDeleteRequest.model_validate_json(req.model_dump_json())
    assert parsed.key == req.key
    assert parsed.op == "cache_delete"


def test_cache_delete_for_inbox_round_trip():
    req = CacheDeleteForInboxRequest(name="linux-fsdevel")
    parsed = CacheDeleteForInboxRequest.model_validate_json(req.model_dump_json())
    assert parsed.name == "linux-fsdevel"


def test_cache_purge_expired_round_trip():
    req = CachePurgeExpiredRequest()
    parsed = CachePurgeExpiredRequest.model_validate_json(req.model_dump_json())
    assert parsed.op == "cache_purge_expired"


def test_ping_round_trip():
    req = PingRequest()
    parsed = PingRequest.model_validate_json(req.model_dump_json())
    assert parsed.op == "ping"


def test_reply_round_trip():
    r = Reply(ok=True)
    parsed = Reply.model_validate_json(r.model_dump_json())
    assert parsed.ok is True
    assert parsed.error is None

    r2 = Reply(ok=False, error="MalformedJSON")
    parsed2 = Reply.model_validate_json(r2.model_dump_json())
    assert parsed2.ok is False
    assert parsed2.error == "MalformedJSON"

    r3 = Reply(ok=True, rows_deleted=42)
    parsed3 = Reply.model_validate_json(r3.model_dump_json())
    assert parsed3.rows_deleted == 42


def test_op_tag_is_load_bearing():
    """Op tag is the discriminator. Decoding a `cache_set`-shaped
    body via `CacheDeleteRequest` must fail; otherwise the broker
    could silently dispatch a set as a delete and lose data."""
    set_payload = json.dumps(
        {
            "op": "cache_set",
            "key": "x",
            "value_json": '"v"',
            "ttl": 60,
        }
    )
    with pytest.raises(ValidationError):
        CacheDeleteRequest.model_validate_json(set_payload)


def test_bootstrap_inboxes_round_trip():
    """Phase 2.0 long op. No args; just the op tag."""
    req = BootstrapInboxesRequest()
    parsed = BootstrapInboxesRequest.model_validate_json(req.model_dump_json())
    assert parsed.op == "bootstrap_inboxes"


def test_reply_result_field_round_trips():
    """Long ops use the `result` dict for op-specific payloads
    (`bootstrap_inboxes` → `{"inboxes": N}`, Phase 2.1 ingest →
    `{"new": N, "linked": N, ...}`). Round-trip a few shapes to
    catch a future pydantic config change that'd accidentally
    drop unknown keys."""
    r = Reply(ok=True, result={"inboxes": 7})
    parsed = Reply.model_validate_json(r.model_dump_json())
    assert parsed.result == {"inboxes": 7}

    r2 = Reply(
        ok=True,
        result={"new": 12, "linked": 3, "dup_batch": 1, "failed": 0},
    )
    parsed2 = Reply.model_validate_json(r2.model_dump_json())
    assert parsed2.result["new"] == 12
    assert parsed2.result["failed"] == 0
