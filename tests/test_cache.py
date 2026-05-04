"""Round-trip coverage for the cache encoder/decoder.

The registry pattern in `mimir/cache.py` is what keeps a class rename
from silently corrupting the on-disk cache: tags are explicit strings,
unknown tags raise, unknown types raise. This test pins the contract
so future drift gets caught the moment someone runs the suite.
"""
import json
from datetime import date, datetime, timezone

import pytest

from mimir.cache import _TAGS, _TYPES, _decode, _encode
from mimir.dashboard import ArchiveStats, DailyVolume
from mimir.threading import ActiveThread


def _roundtrip(value):
    """JSON-encode and decode, the way `cache.set/get` would."""
    return _decode(json.loads(json.dumps(_encode(value))))


def test_registry_has_expected_tags():
    assert set(_TYPES.keys()) == {"ActiveThread", "ArchiveStats", "DailyVolume"}
    assert _TAGS[ActiveThread] == "ActiveThread"
    assert _TAGS[ArchiveStats] == "ArchiveStats"
    assert _TAGS[DailyVolume] == "DailyVolume"


@pytest.mark.parametrize("value", [None, True, False, 0, 1, -1, 1.5, "", "hello"])
def test_primitives_passthrough(value):
    assert _roundtrip(value) == value


def test_datetime_keeps_tz():
    dt = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    out = _roundtrip(dt)
    assert out == dt
    assert out.tzinfo is not None


def test_date_roundtrip():
    assert _roundtrip(date(2025, 1, 2)) == date(2025, 1, 2)


def test_tuple_stays_tuple():
    t = (1, 2, "three", date(2025, 1, 1))
    out = _roundtrip(t)
    assert out == t
    assert isinstance(out, tuple)


def test_list_stays_list():
    out = _roundtrip([1, 2, 3])
    assert out == [1, 2, 3]
    assert isinstance(out, list)


def test_dict_roundtrip():
    assert _roundtrip({"a": 1, "b": "hello"}) == {"a": 1, "b": "hello"}


def test_active_thread_full():
    at = ActiveThread(
        id=1, inbox_name="lkml", message_id="abc@x", subject="s", author="a",
        date=datetime(2025, 1, 1, tzinfo=timezone.utc),
        recent_count=2, reply_count=1,
        last_activity=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    assert _roundtrip(at) == at


def test_active_thread_with_nones():
    at = ActiveThread(
        id=1, inbox_name="lkml", message_id="abc@x", subject=None, author=None,
        date=None, recent_count=0, reply_count=0, last_activity=None,
    )
    assert _roundtrip(at) == at


def test_archive_stats_full():
    stats = ArchiveStats(
        total=100, epochs=3,
        first_date=datetime(2020, 1, 1, tzinfo=timezone.utc),
        last_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    assert _roundtrip(stats) == stats


def test_archive_stats_empty():
    stats = ArchiveStats(total=0, epochs=0, first_date=None, last_date=None)
    assert _roundtrip(stats) == stats


def test_daily_volume_preserves_inner_tuples():
    dv = DailyVolume(
        days=[(date(2025, 1, 1), 5), (date(2025, 1, 2), 0), (date(2025, 1, 3), 10)],
        max_count=10,
    )
    out = _roundtrip(dv)
    assert out == dv
    assert all(isinstance(d, tuple) for d in out.days)


def test_list_of_dataclasses():
    items = [
        ActiveThread(
            id=i, inbox_name="lkml", message_id=f"m{i}@x",
            subject=None, author=None, date=None,
            recent_count=0, reply_count=0, last_activity=None,
        )
        for i in range(3)
    ]
    assert _roundtrip(items) == items


def test_unknown_type_raises():
    class Custom:
        pass

    with pytest.raises(TypeError, match="don't know how to encode"):
        _encode(Custom())


def test_unknown_tag_raises():
    with pytest.raises(ValueError, match="unknown tag"):
        _decode({"__t": "DoesNotExist", "v": {}})
