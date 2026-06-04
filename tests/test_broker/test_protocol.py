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
    req = CacheSetRequest(
        rpc_id=1, key="v2:archive_stats:lkml", value_json='{"x":1}', ttl=86400
    )
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
        CacheSetRequest(rpc_id=1, key="x", value_json="", ttl=60)


def test_cache_set_rejects_negative_ttl():
    with pytest.raises(ValidationError):
        CacheSetRequest(rpc_id=1, key="x", value_json='"v"', ttl=-1)


def test_cache_delete_round_trip():
    req = CacheDeleteRequest(rpc_id=1, key="v2:thing:y")
    parsed = CacheDeleteRequest.model_validate_json(req.model_dump_json())
    assert parsed.key == req.key
    assert parsed.op == "cache_delete"


def test_cache_delete_for_inbox_round_trip():
    req = CacheDeleteForInboxRequest(rpc_id=1, name="linux-fsdevel")
    parsed = CacheDeleteForInboxRequest.model_validate_json(req.model_dump_json())
    assert parsed.name == "linux-fsdevel"


def test_cache_purge_expired_round_trip():
    req = CachePurgeExpiredRequest(rpc_id=1)
    parsed = CachePurgeExpiredRequest.model_validate_json(req.model_dump_json())
    assert parsed.op == "cache_purge_expired"


def test_ping_round_trip():
    req = PingRequest(rpc_id=1)
    parsed = PingRequest.model_validate_json(req.model_dump_json())
    assert parsed.op == "ping"


def test_reply_round_trip():
    r = Reply(rpc_id=1, ok=True)
    parsed = Reply.model_validate_json(r.model_dump_json())
    assert parsed.ok is True
    assert parsed.error is None

    r2 = Reply(rpc_id=1, ok=False, error="MalformedJSON")
    parsed2 = Reply.model_validate_json(r2.model_dump_json())
    assert parsed2.ok is False
    assert parsed2.error == "MalformedJSON"

    r3 = Reply(rpc_id=1, ok=True, rows_deleted=42)
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
    req = BootstrapInboxesRequest(rpc_id=1)
    parsed = BootstrapInboxesRequest.model_validate_json(req.model_dump_json())
    assert parsed.op == "bootstrap_inboxes"


def test_reply_result_field_round_trips():
    """Long ops use the `result` dict for op-specific payloads
    (`bootstrap_inboxes` → `{"inboxes": N}`, Phase 2.1 ingest →
    `{"new": N, "linked": N, ...}`). Round-trip a few shapes to
    catch a future pydantic config change that'd accidentally
    drop unknown keys."""
    r = Reply(rpc_id=1, ok=True, result={"inboxes": 7})
    parsed = Reply.model_validate_json(r.model_dump_json())
    assert parsed.result == {"inboxes": 7}

    r2 = Reply(
        rpc_id=1,
        ok=True,
        result={"new": 12, "linked": 3, "dup_batch": 1, "failed": 0},
    )
    parsed2 = Reply.model_validate_json(r2.model_dump_json())
    assert parsed2.result["new"] == 12
    assert parsed2.result["failed"] == 0


# Task 5 of the fast/slow tier split (spec §2 §5): `WarmInboxRequest`
# gains a `priority` field; `WarmGlobalRequest` gains both `priority`
# and `targets`. Defaults are chosen so existing callers see no
# behaviour change (`priority=1` slow = today's single-tier shape;
# `targets=None` = run every target).


def test_warm_inbox_request_priority_field_round_trips():
    """WarmInboxRequest.priority defaults to 1 (slow); 0=fast.
    JSON round-trips correctly."""
    from mimir.broker.protocol import WarmInboxRequest

    default = WarmInboxRequest(rpc_id=1, inbox_name="alpha")
    assert default.priority == 1

    fast = WarmInboxRequest(rpc_id=1, inbox_name="alpha", priority=0)
    assert fast.priority == 0

    raw = fast.model_dump_json()
    parsed = WarmInboxRequest.model_validate_json(raw)
    assert parsed.priority == 0


def test_warm_inbox_request_priority_rejects_out_of_range():
    """priority is bounded [0, 1] (two tiers; widening to N requires
    a deliberate spec change). Anything else is rejected at the
    wire boundary."""
    from mimir.broker.protocol import WarmInboxRequest

    with pytest.raises(ValidationError):
        WarmInboxRequest(rpc_id=1, inbox_name="alpha", priority=2)
    with pytest.raises(ValidationError):
        WarmInboxRequest(rpc_id=1, inbox_name="alpha", priority=-1)


def test_warm_global_request_priority_and_targets_round_trip():
    """WarmGlobalRequest gains both `priority` (default 1) and
    `targets` (default None). JSON round-trips."""
    from mimir.broker.protocol import WarmGlobalRequest

    default = WarmGlobalRequest(rpc_id=1)
    assert default.priority == 1
    assert default.targets is None

    fast_with_targets = WarmGlobalRequest(
        rpc_id=1, priority=0, targets=["sitemap:index", "sitemap:meta"]
    )
    raw = fast_with_targets.model_dump_json()
    parsed = WarmGlobalRequest.model_validate_json(raw)
    assert parsed.priority == 0
    assert parsed.targets == ["sitemap:index", "sitemap:meta"]


def test_warm_global_request_priority_rejects_out_of_range():
    from mimir.broker.protocol import WarmGlobalRequest

    with pytest.raises(ValidationError):
        WarmGlobalRequest(rpc_id=1, priority=2)
    with pytest.raises(ValidationError):
        WarmGlobalRequest(rpc_id=1, priority=-1)


def test_warm_subsystem_request_round_trip():
    """WarmSubsystemRequest carries inbox_name + subsystem_id +
    priority (default 1, slow). JSON round-trips."""
    from mimir.broker.protocol import WarmSubsystemRequest

    default = WarmSubsystemRequest(rpc_id=1, inbox_name="alpha", subsystem_id=42)
    assert default.priority == 1
    assert default.subsystem_id == 42

    fast = WarmSubsystemRequest(
        rpc_id=1, inbox_name="alpha", subsystem_id=42, priority=0
    )
    raw = fast.model_dump_json()
    parsed = WarmSubsystemRequest.model_validate_json(raw)
    assert parsed.priority == 0
    assert parsed.subsystem_id == 42
    assert parsed.op == "warm_subsystem"


def test_warm_subsystem_request_priority_rejects_out_of_range():
    """priority is bounded [0, 1] (same shape as WarmInboxRequest)."""
    from mimir.broker.protocol import WarmSubsystemRequest

    with pytest.raises(ValidationError):
        WarmSubsystemRequest(rpc_id=1, inbox_name="alpha", subsystem_id=1, priority=2)
    with pytest.raises(ValidationError):
        WarmSubsystemRequest(rpc_id=1, inbox_name="alpha", subsystem_id=1, priority=-1)


def test_warm_subsystem_request_rejects_zero_subsystem_id():
    """subsystem_id is a positive int (sqlite autoincrement starts at
    1); reject 0 / negative at the wire boundary."""
    from mimir.broker.protocol import WarmSubsystemRequest

    with pytest.raises(ValidationError):
        WarmSubsystemRequest(rpc_id=1, inbox_name="alpha", subsystem_id=0)


def test_reply_requires_rpc_id():
    """Reply MUST carry rpc_id so the client can demux concurrent
    in-flight RPCs. Missing field raises pydantic ValidationError."""
    with pytest.raises(ValidationError):
        Reply(ok=True)


def test_reply_rpc_id_round_trips():
    """Reply preserves rpc_id through JSON encode/decode."""
    reply = Reply(ok=True, rpc_id=42)
    decoded = Reply.model_validate_json(reply.model_dump_json())
    assert decoded.rpc_id == 42


def test_cache_set_request_requires_rpc_id():
    """Every broker Request MUST carry rpc_id (3.0.0 wire-protocol
    change). The CacheSetRequest is the canary; the full sweep
    lands in the next task. Missing rpc_id raises ValidationError."""
    with pytest.raises(ValidationError):
        CacheSetRequest(key="x", value_json='"v"', ttl=60)


def test_cache_set_request_rpc_id_round_trips():
    req = CacheSetRequest(rpc_id=7, key="x", value_json='"v"', ttl=60)
    decoded = CacheSetRequest.model_validate_json(req.model_dump_json())
    assert decoded.rpc_id == 7
