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
from mimir.dashboard import ArchiveStats, ArticleSummary, DailyVolume, MonthlyVolume
from mimir.threading import ActiveThread


def _roundtrip(value):
    """JSON-encode and decode, the way `cache.set/get` would."""
    return _decode(json.loads(json.dumps(_encode(value))))


def test_registry_has_expected_tags():
    assert set(_TYPES.keys()) == {
        "ActiveThread", "ArchiveStats", "ArticleSummary", "DailyVolume", "MonthlyVolume",
    }
    assert _TAGS[ActiveThread] == "ActiveThread"
    assert _TAGS[ArchiveStats] == "ArchiveStats"
    assert _TAGS[ArticleSummary] == "ArticleSummary"
    assert _TAGS[DailyVolume] == "DailyVolume"
    assert _TAGS[MonthlyVolume] == "MonthlyVolume"


def test_article_summary_roundtrip():
    from datetime import datetime, timezone

    s = ArticleSummary(
        id=42, subject="patch", author="Foo <foo@bar>",
        date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    assert _roundtrip(s) == s


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


def test_monthly_volume_roundtrip():
    mv = MonthlyVolume(year=2024, months=[(m, m * 10) for m in range(1, 13)], total=780)
    out = _roundtrip(mv)
    assert out == mv
    assert all(isinstance(t, tuple) for t in out.months)


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


def test_delete_for_inbox_pattern_boundary():
    """`delete_for_inbox` must match exactly the inbox name, not
    prefixes that share its leading characters.

    Uses sentinel inbox names that won't collide with anything a
    running mimir process might have cached for the real `lkml` /
    `linux-fsdevel` inboxes.
    """
    from sqlalchemy import delete as sql_delete

    from mimir.cache import delete_for_inbox, set as cache_set
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    target = "xtest-target"
    sibling = "xtest-target-sibling"  # shares the `xtest-target` prefix

    sentinels = {
        # Two for the target inbox: trailing-segment + middle-segment forms.
        f"archive_stats:{target}": 1,
        f"daily_volume:{target}:30": 2,
        # Two for the sibling that shares the leading prefix. Must NOT
        # be deleted by an invalidation of the target.
        f"archive_stats:{sibling}": 3,
        f"daily_volume:{sibling}:30": 4,
        # A key that contains the target name inside another segment,
        # not as a colon-bounded inbox segment. Must NOT be deleted.
        f"misc:somebody-{target}-fanclub:1": 5,
    }
    for k, v in sentinels.items():
        cache_set(k, v, ttl=3600)

    try:
        delete_for_inbox(target)

        # `cache.keys()` strips the internal namespace prefix so we
        # can compare against the keys we passed to `cache.set()`.
        from mimir.cache import keys as cache_keys

        survivors = set(cache_keys()) & set(sentinels.keys())
        assert survivors == {
            f"archive_stats:{sibling}",
            f"daily_volume:{sibling}:30",
            f"misc:somebody-{target}-fanclub:1",
        }
    finally:
        # Sentinels are stored under the namespace prefix; drop the
        # namespaced form from the underlying table.
        from mimir.cache import _ns
        with SessionLocal() as session:
            session.execute(
                sql_delete(CacheEntry).where(
                    CacheEntry.key.in_([_ns(k) for k in sentinels.keys()])
                )
            )
            session.commit()
