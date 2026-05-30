"""Round-trip coverage for the cache encoder/decoder.

The registry pattern in `mimir/cache.py` is what keeps a class rename
from silently corrupting the on-disk cache: tags are explicit strings,
unknown tags raise, unknown types raise. This test pins the contract
so future drift gets caught the moment someone runs the suite.
"""

import json
import logging
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from mimir import cache
from mimir.cache import _TAGS, _TYPES, _decode, _encode
from mimir.dashboard import ArchiveStats, ArticleSummary, DailyVolume, MonthlyVolume
from mimir.threading import ActiveThread


def _roundtrip(value):
    """JSON-encode and decode, the way `cache.set/get` would."""
    return _decode(json.loads(json.dumps(_encode(value))))


def test_registry_has_expected_tags():
    """The five currently-registered cached dataclasses must round-
    trip cleanly. Subset rather than equality so that adding a new
    cached dataclass tomorrow doesn't fail this test; the per-class
    `_TAGS[X]` assertions still catch the removal regression
    (a dropped class would `KeyError` on lookup)."""
    assert {
        "ActiveThread",
        "ArchiveStats",
        "ArticleSummary",
        "DailyVolume",
        "MonthlyVolume",
    }.issubset(_TYPES.keys())
    assert _TAGS[ActiveThread] == "ActiveThread"
    assert _TAGS[ArchiveStats] == "ArchiveStats"
    assert _TAGS[ArticleSummary] == "ArticleSummary"
    assert _TAGS[DailyVolume] == "DailyVolume"
    assert _TAGS[MonthlyVolume] == "MonthlyVolume"


def test_article_summary_roundtrip():
    from datetime import datetime, timezone

    s = ArticleSummary(
        id=42,
        subject="patch",
        author="Foo <foo@bar>",
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


def test_datetime_keeps_non_utc_offset():
    """isoformat() preserves the original UTC offset; the encoder
    must not silently normalise to UTC. lkml ingest sees -0000 and
    +0900 alike; serialising those as the same wall-clock with a
    rewritten offset would corrupt the audit trail."""
    from datetime import timedelta

    tz_plus_2 = timezone(timedelta(hours=2))
    tz_minus_5 = timezone(timedelta(hours=-5, minutes=-30))  # exotic offset
    for tz in (tz_plus_2, tz_minus_5):
        dt = datetime(2025, 1, 2, 3, 4, 5, tzinfo=tz)
        out = _roundtrip(dt)
        assert out == dt
        assert out.utcoffset() == tz.utcoffset(None), (
            f"offset changed across roundtrip: {out.utcoffset()} vs {tz.utcoffset(None)}"
        )


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
        id=1,
        inbox_name="lkml",
        message_id="abc@x",
        subject="s",
        author="a",
        date=datetime(2025, 1, 1, tzinfo=timezone.utc),
        recent_count=2,
        reply_count=1,
        last_activity=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    assert _roundtrip(at) == at


def test_active_thread_with_nones():
    at = ActiveThread(
        id=1,
        inbox_name="lkml",
        message_id="abc@x",
        subject=None,
        author=None,
        date=None,
        recent_count=0,
        reply_count=0,
        last_activity=None,
    )
    assert _roundtrip(at) == at


def test_archive_stats_full():
    stats = ArchiveStats(
        total=100,
        epochs=3,
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
            id=i,
            inbox_name="lkml",
            message_id=f"m{i}@x",
            subject=None,
            author=None,
            date=None,
            recent_count=0,
            reply_count=0,
            last_activity=None,
        )
        for i in range(3)
    ]
    assert _roundtrip(items) == items


def test_nested_dict_of_list_of_dataclasses():
    """The encoder recurses through dicts, lists, and tuples; verify
    a non-flat shape (dict-of-list-of-dataclass) round-trips, since
    every helper that hands data to the cache may build one. The
    existing primitive + flat-dataclass tests don't exercise the
    cross-product."""
    payload = {
        "alpha": [
            ActiveThread(
                id=1,
                inbox_name="alpha",
                message_id="a1@x",
                subject="t1",
                author="a",
                date=None,
                recent_count=1,
                reply_count=0,
                last_activity=None,
            ),
            ActiveThread(
                id=2,
                inbox_name="alpha",
                message_id="a2@x",
                subject="t2",
                author="b",
                date=None,
                recent_count=0,
                reply_count=0,
                last_activity=None,
            ),
        ],
        "beta": [],
    }
    out = _roundtrip(payload)
    assert out == payload
    assert isinstance(out["alpha"][0], ActiveThread)
    assert isinstance(out["beta"], list)


def test_dataclass_containing_nested_dict_roundtrip():
    """ArchiveStats-like containers that *embed* dataclass fields
    inside their own dataclass body work; verify the recursion lands
    when a plain dict-of-dataclass is wrapped one level deeper."""
    items = [
        ArticleSummary(
            id=i,
            subject=f"subj-{i}",
            author=f"u{i}@x",
            date=None,
        )
        for i in range(3)
    ]
    payload = {"recent": items, "meta": {"count": 3}}
    out = _roundtrip(payload)
    assert out == payload
    assert all(isinstance(it, ArticleSummary) for it in out["recent"])


def test_pydantic_basemodel_roundtrip():
    """Pydantic models are encoded by enumerating `model_fields` (not
    by `model_dump()`-flattening) so nested registered types keep
    their tags. RelatedPatch is the first BaseModel that flows through
    the cache (per-subsystem dashboard's recent-articles surface);
    breaking this roundtrip would silently corrupt that cache."""
    from mimir.subsystems import RelatedPatch

    rp = RelatedPatch(
        article_id=42,
        message_id="msg@example.com",
        subject="patch subject",
        author="A. Hacker",
        date=datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc),
        inbox_name="lkml",
    )
    out = _roundtrip(rp)
    assert out == rp
    # Make sure the date came back tz-aware UTC, not as a string.
    assert out.date == rp.date
    assert out.date.tzinfo is not None


def test_pydantic_basemodel_with_none_fields():
    """Subject and author are nullable on RelatedPatch; an article
    with no extractable subject/author must still round-trip."""
    from mimir.subsystems import RelatedPatch

    rp = RelatedPatch(
        article_id=1,
        message_id="bare@example.com",
        subject=None,
        author=None,
        date=None,
        inbox_name="lkml",
    )
    assert _roundtrip(rp) == rp


def test_unknown_type_raises():
    class Custom:
        pass

    with pytest.raises(TypeError, match="don't know how to encode"):
        _encode(Custom())


def test_unknown_tag_raises():
    with pytest.raises(ValueError, match="unknown tag"):
        _decode({"__t": "DoesNotExist", "v": {}})


def test_set_propagates_encoder_typeerror():
    """`set()`'s swallow window is exactly `OperationalError` from
     DB write contention (see CONTEXT.md "Cross-process cache"). An
     encoder error (`TypeError` from `_encode` on an unregistered
     type) is a programming bug: a forgotten `register()` call, a
     typo in the tag, a refactor that dropped a dataclass from the
     registry. Swallowing those would leave the cache silently empty
    , every page recomputes forever, the dev sees nothing in logs,
     the bug ships.

     Pin the boundary: an unregistered class fed to `cache.set` must
     propagate `TypeError` to the caller, and the no-cache-row
     invariant must hold (failed write didn't half-commit anything
     via a different path)."""

    class Unregistered:
        pass

    key = "xtest-encoder-typeerror-key"
    with pytest.raises(TypeError, match="don't know how to encode"):
        cache.set(key, Unregistered(), ttl=60)

    # No row was written: `_encode` raised before the DB call.
    assert cache.get(key) is None


def test_set_propagates_value_inside_collection():
    """An unregistered class buried inside a list/dict still raises,
    because `_encode` recurses through containers. Pins that the
    raise-loudly contract applies to nested values too, not just to
    bare unregistered objects."""

    class Unregistered:
        pass

    key = "xtest-encoder-nested-key"
    with pytest.raises(TypeError, match="don't know how to encode"):
        cache.set(key, {"items": [1, 2, Unregistered()]}, ttl=60)
    assert cache.get(key) is None


def test_set_swallows_operational_error_on_lock(monkeypatch):
    """Unit-level: a locked DB during a cache write must not propagate
    , the request has already rendered. `set()` logs at warning and
     returns. Mocked SessionLocal pins the swallow + log code path
     without depending on real SQLite contention timing."""

    class _LockedSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, _stmt):
            return None

        def commit(self):
            raise OperationalError("INSERT", None, Exception("database is locked"))

    monkeypatch.setattr(cache, "SessionLocal", lambda: _LockedSession())

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

    handler = _Capture(level=logging.WARNING)
    logger = logging.getLogger("mimir.cache")
    logger.addHandler(handler)
    try:
        cache.set("xtest-locked-key", "value", ttl=60)  # must not raise
    finally:
        logger.removeHandler(handler)

    assert any("cache write failed" in r.getMessage() for r in captured), (
        f"expected a 'cache write failed' warning, got {[r.getMessage() for r in captured]}"
    )


def test_delete_swallows_operational_error_on_lock(monkeypatch):
    """`cache.delete` matches `cache.set`'s best-effort posture: an
    `OperationalError` (database is locked, VACUUM, etc.) is swallowed
    + logged, returns 0. Admin CRUD callers must not 500 on a
    successfully-completed DB change just because the cache
    invalidation lost the lock race. Audit (2026-05-15) flagged the
    asymmetry."""

    class _LockedSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, _stmt):
            return None

        def commit(self):
            raise OperationalError("DELETE", None, Exception("database is locked"))

    monkeypatch.setattr(cache, "SessionLocal", lambda: _LockedSession())

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

    handler = _Capture(level=logging.WARNING)
    logger = logging.getLogger("mimir.cache")
    logger.addHandler(handler)
    try:
        result = cache.delete("xtest-locked-delete-key")  # must not raise
    finally:
        logger.removeHandler(handler)

    assert result == 0
    assert any("cache delete failed" in r.getMessage() for r in captured), (
        f"expected a 'cache delete failed' warning, got "
        f"{[r.getMessage() for r in captured]}"
    )


def test_delete_for_inbox_swallows_operational_error_on_lock(monkeypatch):
    """Symmetric to `test_delete_swallows_operational_error_on_lock`
    for the bulk delete_for_inbox path that runs on every inbox
    rename / delete."""

    class _LockedSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, _stmt):
            return None

        def commit(self):
            raise OperationalError("DELETE", None, Exception("database is locked"))

    monkeypatch.setattr(cache, "SessionLocal", lambda: _LockedSession())

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

    handler = _Capture(level=logging.WARNING)
    logger = logging.getLogger("mimir.cache")
    logger.addHandler(handler)
    try:
        result = cache.delete_for_inbox("some-inbox")  # must not raise
    finally:
        logger.removeHandler(handler)

    assert result == 0
    assert any("cache delete_for_inbox failed" in r.getMessage() for r in captured), (
        f"expected a 'cache delete_for_inbox failed' warning, got "
        f"{[r.getMessage() for r in captured]}"
    )


def test_set_swallows_real_sqlite_lock_with_busy_timeout_zero():
    """Integration-level companion to the mocked swallow test: open a
    separate SQLite connection, hold an EXCLUSIVE transaction, then
    call `cache.set()` against a connection with `busy_timeout=0`.
    The real contention raises a real OperationalError, and `set()`
    must still swallow it.

    The mocked version proves the code path; this version proves
    the swallow actually triggers under SQLite-level lock contention,
    which is the scenario the contract describes."""
    import sqlite3

    from mimir.extensions import engine

    db_path = engine.url.database
    if not db_path or db_path == ":memory:":
        pytest.skip("requires file-backed SQLite for cross-connection lock")

    # Hold an EXCLUSIVE transaction on the cache table from a
    # second connection. cache.set's own connection will hit
    # OperationalError on commit/upsert.
    blocker = sqlite3.connect(db_path)
    try:
        # busy_timeout 0 on the blocker so it returns immediately if
        # it ever needs to wait; we never query through it.
        blocker.execute("PRAGMA busy_timeout = 0")
        blocker.execute("BEGIN EXCLUSIVE")

        # Lower the engine's busy_timeout to 0 for this test so we
        # don't wait the production 5s window.
        with engine.connect() as conn:
            conn.execute(text("PRAGMA busy_timeout = 0"))
            conn.commit()

        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(record)

        handler = _Capture(level=logging.WARNING)
        logger = logging.getLogger("mimir.cache")
        logger.addHandler(handler)
        try:
            cache.set("xtest-real-lock-key", "value", ttl=60)  # must not raise
        finally:
            logger.removeHandler(handler)

        # Either we got the swallow warning, or the write succeeded
        # despite the lock (some SQLite versions do non-blocking
        # writes via WAL). Both are acceptable; the contract is
        # specifically "no exception propagates."
        # The "cache write failed" warning IS expected here under
        # EXCLUSIVE lock + busy_timeout 0; assert it appeared.
        assert any("cache write failed" in r.getMessage() for r in captured), (
            "expected a 'cache write failed' warning under real lock; "
            f"got {[r.getMessage() for r in captured]}"
        )
    finally:
        blocker.rollback()
        blocker.close()


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


def test_namespace_version_isolates_stale_rows():
    """A bump to NAMESPACE_VERSION must orphan every cached row from
    the old version. The contract is: rows written under `v<N>:` are
    invisible to a process running with `NAMESPACE_VERSION = N + 1`.

    Simulate the post-bump state by inserting a row with a stale
    prefix and verifying `cache.get()` (which prefixes the lookup with
    the current version) does NOT see it.
    """
    import datetime as _dt
    from sqlalchemy import delete as sql_delete

    from mimir.cache import NAMESPACE_VERSION
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    stale_prefix = f"v{NAMESPACE_VERSION - 1}:"
    stale_key = stale_prefix + "xtest-stale-row"
    expires_at = int(
        (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)).timestamp()
    )
    with SessionLocal() as session:
        session.execute(sql_delete(CacheEntry).where(CacheEntry.key == stale_key))
        session.add(
            CacheEntry(
                key=stale_key,
                value='"stale-value"',
                expires_at=expires_at,
            )
        )
        session.commit()

    try:
        # The current-namespace get must see nothing -- the bare key
        # gets prefixed with v{CURRENT}, which doesn't match v{OLD}:.
        assert cache.get("xtest-stale-row") is None
    finally:
        with SessionLocal() as session:
            session.execute(sql_delete(CacheEntry).where(CacheEntry.key == stale_key))
            session.commit()


# --------------------------------------------------------------------------
# Cache helpers previously covered only indirectly (or not at all):
# `purge_expired` runs in the warm-cache cron and the test suite never
# exercised it; `get_or_compute`'s cache-hit branch was unreached because
# every dashboard test passes `force=True`.
# --------------------------------------------------------------------------


def _seconds_from_now(delta_seconds: int) -> int:
    """Helper for inserting raw CacheEntry rows with a chosen expiry."""
    from datetime import datetime, timezone, timedelta

    return int(
        (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).timestamp()
    )


def test_purge_expired_drops_expired_rows():
    """`purge_expired` deletes every row whose `expires_at` is in the
    past, returns the count, and leaves live rows alone. Without
    this, the cache table grows monotonically as dated keys
    (`threads_for_day:<inbox>:<YYYY-MM-DD>`, `monthly_volume:...`)
    accumulate."""
    from sqlalchemy import delete as sql_delete
    from mimir.cache import _ns, purge_expired
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    expired_keys = ["xtest-expired-1", "xtest-expired-2"]
    live_keys = ["xtest-live-1"]

    with SessionLocal() as s:
        # Clean any prior sentinel rows so the count is exact.
        s.execute(
            sql_delete(CacheEntry).where(
                CacheEntry.key.in_([_ns(k) for k in expired_keys + live_keys])
            )
        )
        for k in expired_keys:
            s.add(
                CacheEntry(key=_ns(k), value='"v"', expires_at=_seconds_from_now(-60))
            )
        for k in live_keys:
            s.add(
                CacheEntry(key=_ns(k), value='"v"', expires_at=_seconds_from_now(3600))
            )
        s.commit()

    try:
        n = purge_expired()
        assert n >= 2, f"expected at least the two seeded expired rows, got {n}"

        with SessionLocal() as s:
            from sqlalchemy import select

            remaining = {
                row.key
                for row in s.execute(
                    select(CacheEntry).where(
                        CacheEntry.key.in_([_ns(k) for k in expired_keys + live_keys])
                    )
                ).scalars()
            }
        # Live key survives; expired keys are gone.
        assert _ns(live_keys[0]) in remaining
        for k in expired_keys:
            assert _ns(k) not in remaining
    finally:
        with SessionLocal() as s:
            s.execute(
                sql_delete(CacheEntry).where(
                    CacheEntry.key.in_([_ns(k) for k in expired_keys + live_keys])
                )
            )
            s.commit()


def test_purge_expired_returns_zero_when_nothing_to_drop():
    """No-rows-to-delete is a valid steady state; `purge_expired`
    returns 0 without raising. Build the meaningful case: at least
    one *live* row exists, none are expired; assert the call returns
    0 AND the live row survives. The older shape ("delete every row
    first, then call purge") was tautological, nothing was there
    to drop because the test had just emptied the table."""
    from sqlalchemy import delete as sql_delete, select
    from mimir.cache import _ns, purge_expired
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    live_key = "xtest-purge-zero-live"
    with SessionLocal() as s:
        s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(live_key)))
        s.add(
            CacheEntry(
                key=_ns(live_key),
                value='"v"',
                expires_at=_seconds_from_now(3600),
            )
        )
        s.commit()

    try:
        assert purge_expired() == 0
        # Live row survives.
        with SessionLocal() as s:
            survived = s.execute(
                select(CacheEntry).where(CacheEntry.key == _ns(live_key))
            ).scalar_one_or_none()
        assert survived is not None
    finally:
        with SessionLocal() as s:
            s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(live_key)))
            s.commit()


def test_get_or_compute_miss_calls_fn_and_stores():
    """First call with a fresh key: `fn()` is invoked, its return is
    persisted via `cache.set()`, and the return value flows through
    to the caller."""
    from sqlalchemy import delete as sql_delete
    from mimir.cache import _ns, get_or_compute
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    key = "xtest-goc-miss"
    with SessionLocal() as s:
        s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
        s.commit()

    calls = 0

    def compute():
        nonlocal calls
        calls += 1
        return {"shape": "fresh", "n": 42}

    try:
        with SessionLocal() as s:
            out = get_or_compute(s, key, ttl=60, fn=compute)
        assert out == {"shape": "fresh", "n": 42}
        assert calls == 1
        # And it landed in the cache (subsequent read sees it).
        assert cache.get(key) == {"shape": "fresh", "n": 42}
    finally:
        with SessionLocal() as s:
            s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
            s.commit()


def test_get_or_compute_hit_skips_fn():
    """A live cache row short-circuits the call to `fn()` -- the
    whole point of cache-aside. This is the path that was previously
    untested because all dashboard tests pass `force=True`."""
    from sqlalchemy import delete as sql_delete
    from mimir.cache import _ns, get_or_compute
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    key = "xtest-goc-hit"
    with SessionLocal() as s:
        s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
        s.commit()
    cache.set(key, {"shape": "warm"}, ttl=3600)

    calls = 0

    def compute():
        nonlocal calls
        calls += 1
        return {"shape": "wrong"}  # if we hit this, the hit path is broken

    try:
        with SessionLocal() as s:
            out = get_or_compute(s, key, ttl=60, fn=compute)
        assert out == {"shape": "warm"}, (
            "cache hit should return the stored value, not call fn"
        )
        assert calls == 0, "fn must not be called on a cache hit"
    finally:
        with SessionLocal() as s:
            s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
            s.commit()


def test_get_or_compute_force_recomputes_despite_live_row():
    """`force=True` bypasses the cache read even when a live row is
    present. This is the warm-cache cron's mechanism for refreshing
    stale-but-not-yet-expired entries after an ingest."""
    from sqlalchemy import delete as sql_delete
    from mimir.cache import _ns, get_or_compute
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    key = "xtest-goc-force"
    with SessionLocal() as s:
        s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
        s.commit()
    cache.set(key, {"shape": "stale"}, ttl=3600)

    calls = 0

    def compute():
        nonlocal calls
        calls += 1
        return {"shape": "fresh"}

    try:
        with SessionLocal() as s:
            out = get_or_compute(s, key, ttl=60, fn=compute, force=True)
        assert out == {"shape": "fresh"}, "force=True must return fn's value"
        assert calls == 1, "force=True must invoke fn even when a row exists"
        # Side effect: the new value is now in the cache for next time.
        assert cache.get(key) == {"shape": "fresh"}
    finally:
        with SessionLocal() as s:
            s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
            s.commit()


def test_refresh_window_recomputes_when_near_expiry():
    """Inside an active `refresh_window`, a still-live row whose
    remaining TTL is less than the window must be recomputed. This is
    what lets warm-cache refresh 24h-TTL keys *before* they expire,
    without recomputing every key on every cron tick."""
    from sqlalchemy import delete as sql_delete
    from mimir.cache import _ns, get_or_compute, refresh_window
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    key = "xtest-refresh-near-expiry"
    with SessionLocal() as s:
        s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
        # 60 s of TTL remaining, but the window asks for "anything
        # within 300 s of expiry", so recompute.
        s.add(
            CacheEntry(
                key=_ns(key),
                value='"stale"',
                expires_at=_seconds_from_now(60),
            )
        )
        s.commit()

    calls = 0

    def compute():
        nonlocal calls
        calls += 1
        return "fresh"

    try:
        with SessionLocal() as s, refresh_window(300):
            out = get_or_compute(s, key, ttl=60, fn=compute)
        assert out == "fresh"
        assert calls == 1, "near-expiry row inside refresh_window must recompute"
    finally:
        with SessionLocal() as s:
            s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
            s.commit()


def test_refresh_window_skips_when_plenty_of_ttl_left():
    """Inside an active `refresh_window`, a row whose remaining TTL
    *exceeds* the window must still be served from cache. This is
    what lets 24h-TTL keys (archive_stats) sit unbothered across most
    5-minute warm-cache ticks."""
    from sqlalchemy import delete as sql_delete
    from mimir.cache import _ns, get_or_compute, refresh_window
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    key = "xtest-refresh-far-from-expiry"
    with SessionLocal() as s:
        s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
        # 1 hour of TTL remaining; window is 5 min, skip recompute.
        s.add(
            CacheEntry(
                key=_ns(key),
                value='"stale-but-fresh-enough"',
                expires_at=_seconds_from_now(3600),
            )
        )
        s.commit()

    calls = 0

    def compute():
        nonlocal calls
        calls += 1
        return "fresh"

    try:
        with SessionLocal() as s, refresh_window(300):
            out = get_or_compute(s, key, ttl=60, fn=compute)
        assert out == "stale-but-fresh-enough"
        assert calls == 0, "row outside refresh_window must short-circuit"
    finally:
        with SessionLocal() as s:
            s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
            s.commit()


def test_get_or_compute_expired_row_treated_as_miss():
    """An expired (but still-present) row must not satisfy a
    `get_or_compute`. Without `force=True`, the helper still falls
    through to `fn()` because the read filter is `expires_at >= now`."""
    from sqlalchemy import delete as sql_delete
    from mimir.cache import _ns, get_or_compute
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    key = "xtest-goc-expired"
    with SessionLocal() as s:
        s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
        # Insert directly with a past expiry so the row exists but
        # is not "live" by the helper's clock.
        s.add(
            CacheEntry(
                key=_ns(key),
                value='"stale"',
                expires_at=_seconds_from_now(-60),
            )
        )
        s.commit()

    calls = 0

    def compute():
        nonlocal calls
        calls += 1
        return "fresh"

    try:
        with SessionLocal() as s:
            out = get_or_compute(s, key, ttl=60, fn=compute)
        assert out == "fresh"
        assert calls == 1, "expired row must not short-circuit the read"
    finally:
        with SessionLocal() as s:
            s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
            s.commit()


# --------------------------------------------------------------------------
# Real-path round-trips for every registered dataclass.
#
# The `_roundtrip` helper above goes encoder → json text → decoder; the
# tests below go through the *real* `cache.set` → SQLite TEXT column →
# `cache.get` path. The two paths diverge whenever a value survives
# `json.dumps`/`json.loads` but trips up SQLite's TEXT encoding
# surrogates being the obvious case. A registered dataclass that
# round-trips via `_roundtrip` but fails the real path would silently
# break dashboard caching in production.
# --------------------------------------------------------------------------


def _real_path_roundtrip(key: str, value):
    """`cache.set` then `cache.get`. Strips the row in the caller's
    `finally:` (use the same key)."""
    cache.set(key, value, ttl=3600)
    return cache.get(key)


def _drop_cache_key(key: str) -> None:
    from sqlalchemy import delete as sql_delete
    from mimir.cache import _ns
    from mimir.extensions import SessionLocal
    from mimir.models import CacheEntry

    with SessionLocal() as s:
        s.execute(sql_delete(CacheEntry).where(CacheEntry.key == _ns(key)))
        s.commit()


def test_real_path_roundtrip_active_thread():
    key = "xtest-real-path-active-thread"
    value = ActiveThread(
        id=1,
        inbox_name="lkml",
        message_id="m1@x",
        subject="s",
        author="a",
        date=datetime(2025, 1, 1, tzinfo=timezone.utc),
        recent_count=2,
        reply_count=1,
        last_activity=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    try:
        assert _real_path_roundtrip(key, value) == value
    finally:
        _drop_cache_key(key)


def test_real_path_roundtrip_article_summary():
    key = "xtest-real-path-article-summary"
    value = ArticleSummary(
        id=42,
        subject="patch",
        author="Foo <foo@bar>",
        date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    try:
        assert _real_path_roundtrip(key, value) == value
    finally:
        _drop_cache_key(key)


def test_real_path_roundtrip_archive_stats():
    key = "xtest-real-path-archive-stats"
    value = ArchiveStats(
        total=100,
        epochs=3,
        first_date=datetime(2020, 1, 1, tzinfo=timezone.utc),
        last_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    try:
        assert _real_path_roundtrip(key, value) == value
    finally:
        _drop_cache_key(key)


def test_real_path_roundtrip_daily_volume():
    key = "xtest-real-path-daily-volume"
    value = DailyVolume(
        days=[(date(2025, 1, 1), 5), (date(2025, 1, 2), 0), (date(2025, 1, 3), 10)],
        max_count=10,
    )
    try:
        out = _real_path_roundtrip(key, value)
        assert out == value
        # Inner items must come back as tuples, not lists, DailyVolume
        # ships them through `tuple(...)` and templates index by attr.
        assert all(isinstance(d, tuple) for d in out.days)
    finally:
        _drop_cache_key(key)


def test_real_path_roundtrip_monthly_volume():
    key = "xtest-real-path-monthly-volume"
    value = MonthlyVolume(
        year=2024,
        months=[(m, m * 10) for m in range(1, 13)],
        total=780,
    )
    try:
        out = _real_path_roundtrip(key, value)
        assert out == value
        assert all(isinstance(t, tuple) for t in out.months)
    finally:
        _drop_cache_key(key)


def test_real_path_roundtrip_string_with_surrogate_escape_range():
    """Surrogate-escape range bytes (U+DC80-U+DCFF) come out of the
    MIME parser when bytes don't decode against the declared charset.
    `parse_message` scrubs them to U+FFFD before persisting, so they
    shouldn't reach the cache. But if scrubbing ever regressed, a
    surrogate-containing subject would land in `ArticleSummary` and
    flow through `cache.set`. Pin the behaviour: either it works (no
    visible production impact), or it raises loudly (a 500 on the
    page that triggered the cache write, not silent corruption).

    Specifically: `json.dumps` with the default `ensure_ascii=True`
    escapes lone surrogates as `\\uXXXX` sequences, which survive the
    SQLite TEXT round-trip and decode back to the same character. So
    the contract is "survives." Locking it in here means a future
    `ensure_ascii=False` performance tweak would loudly break this
    test rather than silently 500 dashboards in production."""
    key = "xtest-real-path-surrogate"
    payload = ArticleSummary(
        id=99,
        # U+DCFF, a surrogate-escape codepoint from a malformed-charset
        # body. Lone surrogate; not part of a valid pair.
        subject="bad encoding \udcff in subject",
        author="a@b.example",
        date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    try:
        out = _real_path_roundtrip(key, payload)
        assert out == payload
    finally:
        _drop_cache_key(key)


def test_cache_get_many_returns_only_unexpired_present_keys():
    cache.set("k1", {"a": 1}, ttl=60)
    cache.set("k2", {"b": 2}, ttl=60)
    # k3 not set
    cache.set("k4", {"d": 4}, ttl=-1)  # already expired
    got = cache.get_many(["k1", "k2", "k3", "k4"])
    assert set(got.keys()) == {"k1", "k2"}
    assert got["k1"] == {"a": 1}
    assert got["k2"] == {"b": 2}


def test_cache_get_many_empty_keys_returns_empty():
    assert cache.get_many([]) == {}


def test_cache_module_has_no_pickle_dependency():
    """Regression marker for the 2026-04-ish pickle-pivot. The original
    cache was a single pickle file; concurrent-write races dropped
    entries and pickle.loads on any code path that could *write* the
    file would have been an RCE surface. The SQLite-JSON design has
    neither problem. Pin "no pickle anywhere in this module" so a
    future contributor reaching for the convenient pickle.dumps
    trips this test instead of merging."""
    import inspect

    src = inspect.getsource(cache)
    assert "pickle" not in src, (
        "mimir.cache must not depend on pickle (concurrent-write races + "
        "RCE surface on read). See CONTEXT.md 'Cross-process cache'."
    )


# --------------------------------------------------------------------------
# Phase 2 warm-path: set_via_writer
# --------------------------------------------------------------------------


def test_cache_set_via_writer_commits_through_writer(seeded_db):
    """set_via_writer() composes a WriteOp wrapping the UPSERT and
    submits to the writer; the returned WriteFuture resolves once
    the writer commits. The row is then readable via cache.get_or_compute()
    from a fresh session."""
    from mimir.broker.writes import WriterThread
    from mimir.cache import get_or_compute, set_via_writer
    from mimir.extensions import SessionLocal

    writer = WriterThread.from_settings()
    writer.start()
    try:
        future = set_via_writer(writer, "test-writer-key", {"v": 1}, ttl=60)
        future.result(timeout=5)

        # Read back via cache.get_or_compute (no compute should fire
        # because the value is cached).
        with SessionLocal() as s:
            value = get_or_compute(
                s,
                "test-writer-key",
                ttl=60,
                fn=lambda: {"v": "MISS"},
            )
            assert value == {"v": 1}
    finally:
        writer.stop(timeout=5)


def test_cache_set_via_writer_overwrites_existing_key(seeded_db):
    """Second set_via_writer on the same key replaces the value
    (UPSERT semantics)."""
    from mimir.broker.writes import WriterThread
    from mimir.cache import get_or_compute, set_via_writer
    from mimir.extensions import SessionLocal

    writer = WriterThread.from_settings()
    writer.start()
    try:
        set_via_writer(writer, "overwrite-key", {"v": 1}, ttl=60).result(timeout=5)
        set_via_writer(writer, "overwrite-key", {"v": 2}, ttl=60).result(timeout=5)

        with SessionLocal() as s:
            value = get_or_compute(
                s,
                "overwrite-key",
                ttl=60,
                fn=lambda: {"v": "MISS"},
            )
            assert value == {"v": 2}
    finally:
        writer.stop(timeout=5)


def test_cache_set_routes_through_active_writer_when_registered(seeded_db):
    """When _context.set_active() has registered a WriterThread,
    cache.set() dispatches the UPSERT through it; the row is
    readable after the writer drains. Verifies by registering a
    writer, calling cache.set, and asserting the row commits."""
    import time as _time

    from sqlalchemy import create_engine, text

    from mimir.broker import _context
    from mimir.broker.pools import ReadSessionPool
    from mimir.broker.writes import WriterThread
    from mimir import cache
    from mimir.config import settings

    pool = ReadSessionPool.from_settings()
    writer = WriterThread.from_settings()
    writer.start()
    try:
        _context.set_active(pool, writer)
        cache.set("via-active-writer-key", {"v": 1}, ttl=60)

        deadline = _time.monotonic() + 5
        engine = create_engine(settings.database_url, future=True)
        try:
            while _time.monotonic() < deadline:
                with engine.connect() as c:
                    # The cache namespace prefix is applied by cache.set,
                    # so the row's key column starts with "v<N>:".
                    row = c.execute(
                        text("SELECT key FROM cache WHERE key LIKE :p"),
                        {"p": "%via-active-writer-key"},
                    ).scalar_one_or_none()
                    if row is not None:
                        return
                _time.sleep(0.05)
            raise AssertionError(
                "cache.set did not commit through the active writer in 5s"
            )
        finally:
            engine.dispose()
    finally:
        _context.clear_active()
        writer.stop(timeout=5)
        pool.close()


def test_cache_set_falls_back_to_inline_when_no_active_writer(seeded_db):
    """Outside the broker (no active writer registered), cache.set
    runs the UPSERT inline as today. The web tier still uses this
    path."""
    from sqlalchemy import create_engine, text

    from mimir.broker import _context
    from mimir import cache
    from mimir.config import settings

    _context.clear_active()
    cache.set("no-active-writer-key", {"v": 2}, ttl=60)
    engine = create_engine(settings.database_url, future=True)
    with engine.connect() as c:
        row = c.execute(
            text("SELECT key FROM cache WHERE key LIKE :p"),
            {"p": "%no-active-writer-key"},
        ).scalar_one_or_none()
        assert row is not None
    engine.dispose()
